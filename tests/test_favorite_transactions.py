"""Confirmed writes use durable operation identities and factual tool receipts."""

import asyncio
import json
import time

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent import idem
from agent.domains.favorites import transactions
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime import writes
from tools.result import fact, ok, tgt


def _grant():
    return {"uid": 7, "conv": 42, "jti": "a" * 32, "exp": int(time.time()) + 60,
            "workflow": "favorite_add", "executor": "favorites",
            "specs": [{"tool": "add_favorite", "args": {"article_id": 19}}]}


def _config():
    return {"configurable": {"thread_id": "conv_42", "conversation_id": 42,
                             "principal": Principal(uid=7, role="user", source=SOURCE_ASSERTION)}}


class Favorite:
    name = "add_favorite"

    def __init__(self):
        self.calls = 0

    def invoke(self, args, config=None):
        self.calls += 1
        assert config["configurable"]["principal"].uid == 7
        return ok("已收藏文章 19。", meta=fact(
            "favorite_add", changed=True, target=tgt("note", 19), article_id=19))


def test_confirmed_write_replays_the_receipt_without_writing_twice(tmp_path):
    ledger = idem.Ledger(str(tmp_path / "writes.sqlite3"))
    tool = Favorite()
    tools = {tool.name: tool}

    first = transactions.execute(_grant(), _config(), None, tools=tools, ledger=ledger)
    second = transactions.execute(_grant(), _config(), None, tools=tools, ledger=ledger)

    assert tool.calls == 1
    assert first["reply"] == second["reply"] == "已收藏文章 19。"
    assert first["receipts"][0]["action"] == second["receipts"][0]["action"]
    assert "收藏" in first["receipts"][0]["action"]
    assert not second["blocked"]


@pytest.mark.parametrize("change", [
    {"uid": 8}, {"conv": 43}, {"exp": 1}, {"jti": ""},
    {"specs": []},
    {"specs": [{"tool": "remove_favorite", "args": {"article_id": 19}}]},
    {"specs": [{"tool": "add_favorite", "args": {"article_id": True}}]},
    {"specs": [{"tool": "add_favorite", "args": {"article_id": 19, "uid": 8}}]},
])
def test_invalid_confirmation_never_opens_a_ledger_or_calls_a_tool(change):
    class NoLedger:
        def claim(self, *args, **kwargs):
            raise AssertionError("invalid confirmation reached the write ledger")

    tool = Favorite()
    outcome = transactions.execute({**_grant(), **change}, _config(), None,
                                   tools={tool.name: tool}, ledger=NoLedger())
    assert outcome["receipts"] == []
    assert outcome["blocked"]
    assert tool.calls == 0


def test_unverified_identity_cannot_execute_a_confirmation():
    config = _config()
    config["configurable"]["principal"] = Principal(uid=7, role="user")
    tool = Favorite()
    result = transactions.execute(_grant(), config, None, tools={tool.name: tool})
    assert result["receipts"] == []
    assert tool.calls == 0


def test_crash_after_write_leaves_unknown_outcome_and_does_not_retry(tmp_path):
    ledger = idem.Ledger(str(tmp_path / "crash.sqlite3"))

    class CrashingFavorite(Favorite):
        def invoke(self, args, config=None):
            self.calls += 1
            raise SystemExit("process stopped after sending the external write")

    tool = CrashingFavorite()
    with pytest.raises(SystemExit):
        transactions.execute(_grant(), _config(), None, tools={tool.name: tool}, ledger=ledger)
    result = transactions.execute(_grant(), _config(), None,
                                  tools={tool.name: tool}, ledger=ledger)
    assert tool.calls == 1
    assert result["receipts"] == []
    assert "结果未知" in result["reply"]


def test_success_text_without_a_fact_receipt_does_not_report_completion(tmp_path):
    class UnverifiedFavorite(Favorite):
        def invoke(self, args, config=None):
            return "已收藏文章 19。"

    result = transactions.execute(_grant(), _config(), None,
                                  tools={"add_favorite": UnverifiedFavorite()},
                                  ledger=idem.Ledger(str(tmp_path / "unverified.sqlite3")))
    assert result["receipts"] == []
    assert result["blocked"]
    assert "无法确认" in result["reply"]


@pytest.mark.parametrize("failure", ["tool", "ledger"])
def test_exception_after_write_keeps_intent_and_prevents_retry(failure, monkeypatch, tmp_path):
    ledger = idem.Ledger(str(tmp_path / "uncertain.sqlite3"))

    class UncertainFavorite(Favorite):
        def invoke(self, args, config=None):
            result = super().invoke(args, config=config)
            if failure == "tool":
                raise RuntimeError("failed after sending the write")
            return result

    def fail_finish(*args):
        raise OSError("receipt storage unavailable")

    if failure == "ledger":
        monkeypatch.setattr(ledger, "finish", fail_finish)
    tool = UncertainFavorite()
    first = transactions.execute(_grant(), _config(), None,
                                 tools={tool.name: tool}, ledger=ledger)
    second = transactions.execute(_grant(), _config(), None,
                                  tools={tool.name: tool}, ledger=ledger)
    assert tool.calls == 1
    assert not first["receipts"] and first["blocked"]
    assert not second["receipts"] and second["blocked"]
    assert "无法确认" in first["reply"] and "结果未知" in second["reply"]


def test_native_executes_confirmed_favorite_without_model_or_legacy(monkeypatch, tmp_path):
    tool = Favorite()
    monkeypatch.setattr(transactions, "get_all_tools", lambda: [tool])
    monkeypatch.setattr(writes, "ledger_for",
                        lambda saver: idem.Ledger(str(tmp_path / "runtime.sqlite3")))
    from agent.runtime.native import NativeRuntime
    from agent.runtime.native_store import NativeConversationStore
    runtime = NativeRuntime(NativeConversationStore(InMemorySaver()))
    state = {"messages": [HumanMessage(content="确认收藏")], "confirm_grant": _grant()}

    first = list(runtime.stream_events(state, _config()))
    second = list(runtime.stream_events(state, _config()))

    assert first[-1].execution["receipts"][0]["tool"] == "add_favorite"
    assert second[-1].execution["receipts"][0]["tool"] == "add_favorite"
    assert tool.calls == 1
    snapshot = runtime.get_state(_config())
    assert snapshot.next == ()
    assert snapshot.values["pending_action"] is None


def test_stream_producer_emits_existing_execution_receipt(monkeypatch, tmp_path):
    import server
    from agent.runtime.native import NativeRuntime
    from agent.runtime.native_store import NativeConversationStore

    tool = Favorite()
    monkeypatch.setattr(transactions, "get_all_tools", lambda: [tool])
    monkeypatch.setattr(writes, "ledger_for",
                        lambda saver: idem.Ledger(str(tmp_path / "stream.sqlite3")))
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: 1 / 0)
    monkeypatch.setattr(server, "_agent",
                        NativeRuntime(NativeConversationStore(InMemorySaver())))

    async def collect():
        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        producer = asyncio.create_task(asyncio.to_thread(
            server._run_agent_stream_to_queue, [HumanMessage(content="确认收藏")],
            "conv_42", queue, loop, user_id=7,
            principal=_config()["configurable"]["principal"],
            conversation_id=42, confirm_grant=_grant()))
        frames = []
        while True:
            frame = await asyncio.wait_for(queue.get(), timeout=5)
            if frame is None:
                break
            frames.append(frame)
        await producer
        return frames

    frames = asyncio.run(collect())
    receipts = [json.loads(frame.removeprefix("__EXEC__:")) for frame in frames
                if isinstance(frame, str) and frame.startswith("__EXEC__:")]
    assert len(receipts) == 1
    assert receipts[0][0]["tool"] == "add_favorite"
    assert receipts[0][0]["args"] == {"article_id": "19"}
    assert "收藏" in receipts[0][0]["action"]
    assert not any(isinstance(frame, Exception) for frame in frames)
    assert tool.calls == 1
