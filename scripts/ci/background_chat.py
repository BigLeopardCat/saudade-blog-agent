"""Exercise the real authenticated host/DBOS/Agent loop with synthetic model replies."""

import time
import uuid

import httpx
from httpx_sse import connect_sse


def eventually(function):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if result := function():
            return result
        time.sleep(0.05)
    raise AssertionError("Background stack did not reach the expected state")


def verify(token, provider):
    with httpx.Client(base_url="http://127.0.0.1:18080", trust_env=False, timeout=35,
                      headers={"Authorization": "Bearer " + token}) as client:
        def call(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        def start(marker):
            cid = call("POST", "/api/chat/conversations")["id"]
            turn = uuid.uuid4().hex
            submission = {"run_id": uuid.uuid4().hex, "request": {
                "conversation_id": cid, "message": marker, "task_id": turn, "turn_id": turn,
                "task_inputs": True, "browser_feedback": True}}
            row = call("POST", "/api/chat/runs", json=submission)
            assert row["run_id"] == submission["run_id"]
            return row, submission

        def events(row, offset=0):
            with connect_sse(client, "GET", f"/api/chat/runs/{row['run_id']}/events",
                             params={"offset": offset}) as source:
                source.response.raise_for_status()
                yield from source.iter_sse()

        def view(row):
            rows = call("GET", "/api/chat/runs", params={
                "conversation_id": row["conversation_id"]})["items"]
            return next(item for item in rows if item["run_id"] == row["run_id"])

        assert call("GET", "/api/chat/runs")["enabled"]
        first, submission = start("STACK_BG_A")
        eventually(lambda: "STACK_BG_A" in provider.status()["entered"])
        reader = events(first)
        event = next(reader)
        offset = int(event.id) + 1
        reader.close()
        assert view(first)["status"] == "PENDING"
        # Repeated submission is observation, never a second host execution.
        assert call("POST", "/api/chat/runs", json=submission)["run_id"] == first["run_id"]
        conflict = {**submission, "request": {**submission["request"], "message": "changed"}}
        assert client.post("/api/chat/runs", json=conflict).status_code == 409
        duplicate = {**submission, "run_id": uuid.uuid4().hex}
        assert client.post("/api/chat/runs", json=duplicate).status_code == 409
        second, _ = start("STACK_BG_B")
        received = list(events(second))
        assert received[-1].event == "done", received
        assert view(first)["status"] == "PENDING"
        provider.background_release.set()
        received = list(events(first, offset))
        assert received[-1].event == "done", received
        for row, marker in ((first, "STACK_BG_A"), (second, "STACK_BG_B")):
            history = call("GET", "/api/chat/history", params={
                "conversation_id": row["conversation_id"]})["items"]
            assert [item["role"] for item in history].count("user") == 1
            answers = [item["content"] for item in history if item["role"] == "assistant"]
            assert len(answers) == 1 and marker + "_FINAL" in answers[0], history
            assert provider.status()["calls"].count(marker) == 1
        stopped, _ = start("STACK_BG_STOP")
        eventually(lambda: "STACK_BG_STOP" in provider.status()["entered"])
        assert call("POST", f"/api/chat/runs/{stopped['run_id']}/cancel")["status"] == "CANCELLED"
        eventually(lambda: "STACK_BG_STOP" in provider.status()["closed"])
        return {"parallel_conversations": True, "detached_execution": True,
                "offset_reconnect": True, "idempotent_submission": True,
                "same_conversation_conflict": True, "history_isolation": True,
                "cancel_closed_model_connection": True}
