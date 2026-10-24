"""Production traces record the same body queued by native transport."""

import asyncio
import json
import secrets
from pathlib import Path

import pytest
from langchain_core.messages import AIMessageChunk

import server
from agent.runtime.results import TurnResult
from utils import trace


def drive(events, monkeypatch, tmp_path, *, failure=None):
    class Runtime:
        def stream_events(self, state, config):
            yield from events
            if failure:
                raise failure

    monkeypatch.setattr(server, "_agent", Runtime())
    identifier = secrets.token_hex(8)

    async def run():
        trace.start_trace(identifier, 7, "conv_42", dir=str(tmp_path),
                          name=identifier, by_day=False)
        queue = asyncio.Queue()
        task = asyncio.create_task(asyncio.to_thread(
            server._run_agent_stream_to_queue, [], "conv_42", queue,
            asyncio.get_running_loop(), user_id=7))
        items = []
        while True:
            item = await asyncio.wait_for(queue.get(), 2)
            items.append(item)
            if item is None or isinstance(item, server._NativeCancelled):
                break
        await task
        return items

    items = asyncio.run(run())
    failed = any(isinstance(item, Exception) for item in items)
    path = trace.finish_trace(identifier, "producer_error" if failed else "producer_done", .1,
                              frames=len(items))
    return items, json.loads(Path(path).read_text())


@pytest.mark.parametrize("chunks", [[], ["你好。"], ["你", "好。"]])
def test_trace_matches_actual_chunks_and_terminal_suffix(monkeypatch, tmp_path, chunks):
    events = [*(AIMessageChunk(content=text) for text in chunks), TurnResult("你好。")]
    items, doc = drive(events, monkeypatch, tmp_path)
    visible = "".join(item.content for item in items if isinstance(item, AIMessageChunk))
    assert doc["reply"] == visible == "你好。"
    assert len([event for event in doc["events"] if event["event"] == "stream_end"]) == 1


def test_card_body_matches_trace_and_credentials_stay_out(monkeypatch, tmp_path):
    opaque = secrets.token_urlsafe(24)
    items, doc = drive([AIMessageChunk(content="前言。"), TurnResult("前言。请确认。", {
        "pending_confirm": {"q": "确认？", "token": opaque, "opts": []},
        "confirm_text": "请确认。"})], monkeypatch, tmp_path)
    assert doc["reply"] == "前言。请确认。"
    assert opaque not in json.dumps(doc)
    assert sum(str(item).startswith("__CONFIRM__:") for item in items) == 1


def test_protocol_failure_records_only_visible_partial_body(monkeypatch, tmp_path):
    items, doc = drive([AIMessageChunk(content="部分"), TurnResult("不一致的终态")],
                       monkeypatch, tmp_path)
    assert doc["reply"] == "部分"
    assert any(isinstance(item, Exception) for item in items)
    assert not any("不一致的终态" in str(item) for item in items)


def test_storage_failure_preserves_partial_trace_without_success(monkeypatch, tmp_path):
    items, doc = drive([AIMessageChunk(content="半句话")], monkeypatch, tmp_path,
                       failure=OSError("private storage failure"))
    assert doc["reply"] == "半句话"
    assert any(isinstance(item, OSError) for item in items)
    assert "private storage failure" not in doc["reply"]
