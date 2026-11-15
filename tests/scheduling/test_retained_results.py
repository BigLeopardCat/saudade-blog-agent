"""A delivered task result remains reportable after workflow-history cleanup."""

from types import SimpleNamespace

from agent.domains.automations.results import read_result
from agent.scheduling.service import ScheduleNotFound
from tools import base


def test_full_workflow_result_remains_the_preferred_complete_source(monkeypatch):
    original = {"state": "success", "result": {"status": "completed", "text": "full result"}}
    monkeypatch.setattr(
        base,
        "_admin_get",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not query a second result source")),
    )
    result = read_result(SimpleNamespace(result=lambda *_: original), None, "schedule", "run", {})
    assert result.data == original


def test_delivered_text_survives_history_cleanup_and_the_final_commit_race(monkeypatch):
    saved = {
        "runId": "run",
        "scheduleId": "schedule",
        "content": "完整结果" * 500,
        "status": "completed",
    }
    monkeypatch.setattr(base, "_admin_get", lambda *_: saved)

    def expired(*args):
        raise ScheduleNotFound("expired")

    for service in [
        SimpleNamespace(result=expired),
        SimpleNamespace(result=lambda *_: {"state": "pending", "result": None}),
    ]:
        result = read_result(service, None, "schedule", "run", {})
        assert result.data["source"] == "delivered_execution_result"
        assert result.data["result"]["text"] == saved["content"]
        assert result.data["state"] == "result_saved"
