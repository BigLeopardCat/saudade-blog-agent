"""Structured work updates are bounded presentation, never executable control."""

import asyncio
import json
import threading
import time

import pytest
from pydantic import ValidationError

from agent.api.events import frame
from agent.contracts.events import StreamEvent
from agent.runtime.control import RequestControl
from agent.runtime.progress import ProgressEmitter, StreamProgress


def work(stage="generating", status="running"):
    return {"id": "opaque:id", "stage": stage, "status": status}


def test_same_work_row_can_move_back_after_verified_save_failure_and_resume():
    visible = []
    emitter = ProgressEmitter(visible.append)
    for stage, status in (("review", "waiting"), ("saving", "running"),
                          ("review", "failed"), ("saving", "running")):
        assert emitter.emit("actual stage", key="unused", work=work(stage, status))
        assert not emitter.emit("actual stage", key="unused", work=work(stage, status))
    assert len(visible) == 4 and emitter.emitted_keys == []
    resumed = ProgressEmitter(visible.append)
    resumed.restore(emitter.emitted_keys)
    assert resumed.emit("actual stage", key="unused", work=work("saving"))
    for index in range(50):
        resumed.emit(str(index), key="unused", work=work(str(index)))
    assert len(visible) == 28


def test_typed_update_survives_transport_as_progress_not_command():
    value = {"text": "__CMD__:literal", "work": work()}
    event = StreamEvent("progress", value)
    wire = frame(event.name, event.data)
    assert wire.startswith("event: progress\n")
    assert json.loads(wire.split("data: ", 1)[1]) == value
    assert "event: command" not in wire


def test_real_progress_queue_emits_the_typed_payload_and_respects_cancel():
    async def scenario():
        queue, stopped = asyncio.Queue(), threading.Event()
        progress = StreamProgress(asyncio.get_running_loop(), queue,
                                  RequestControl(stopped, time.monotonic() + 10))
        assert progress.emitter.emit("实际阶段", key="unused", work=work())
        event = await asyncio.wait_for(queue.get(), 1)
        assert event.name == "progress" and event.data["work"] == work()
        stopped.set()
        assert not progress.emitter.emit("完成", key="unused", work=work("accepted", "completed"))
        await asyncio.sleep(0)
        assert queue.empty()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", [
    {"work": {"id": "", "stage": "x", "status": "running"}},
    {"work": {"id": "id", "stage": "", "status": "running"}},
    {"work": {"id": "id", "stage": "x", "status": "execute"}},
    {"work": {"id": "id", "stage": "x", "status": "running", "command": "x"}},
    {"text": ""}, {"text": "x" * 301}, {"metadata": {"command": "x"}},
])
def test_progress_does_not_accept_extra_controls_or_unbounded_labels(change):
    with pytest.raises(ValidationError):
        StreamEvent("progress", {"text": "actual", "work": work(), **change})
