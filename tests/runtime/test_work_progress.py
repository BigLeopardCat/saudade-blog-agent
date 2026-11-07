"""Structured work updates are bounded presentation, never executable control."""

import json

import pytest
from pydantic import ValidationError

from agent.api.events import frame
from agent.contracts.events import StreamEvent
from agent.runtime.progress import ProgressEmitter


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
