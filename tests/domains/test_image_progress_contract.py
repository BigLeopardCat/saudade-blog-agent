"""Python progress events and the task viewer agree on durable image states."""
import json
from pathlib import Path

import pytest

from agent.domains.image_generation.state import Job, show_progress

STATES = json.loads((Path(__file__).parents[2] /
    "frontend/tests/fixtures/image-job-states.json").read_text())


@pytest.mark.parametrize("row", STATES)
def test_image_state_is_not_a_fabricated_percentage_or_premature_completion(monkeypatch, row):
    events = []
    monkeypatch.setattr("agent.runtime.progress.emit", lambda config, text, **kwargs:
                        events.append({"text": text, **kwargs}))
    job = Job.model_validate({"id": "6b9e2bf1-56af-428c-8177-e3ad0641e036",
        "requestHash": "a" * 64, "status": row["status"],
        "imageRemoved": row.get("imageRemoved", False), "error": row.get("error"),
        "imageId": 31 if row["status"] == "accepted" else None,
        "request": {"provider": "https://image.test/v1", "model": "fixture", "prompt": "prompt",
                    "size": "1536x1024", "quality": "high", "references": []}})
    show_progress(job, {})
    assert events[0]["text"] == row["text"]
    assert events[0]["work"]["stage"] == row["status"]
    assert events[0]["work"]["status"] == row["workStatus"]
