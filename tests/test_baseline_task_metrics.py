"""Case attribution uses captured request evidence and actual task accounting."""

from contextlib import contextmanager

import httpx

from eval import online_baseline


def test_baseline_links_task_costs_to_cases_by_captured_request_ids(tmp_path, monkeypatch):
    from test_task_metrics import trace

    path = trace(tmp_path, "captured-request")

    class Fake:
        agent_url = "http://agent"
        requests = [{"trace": str(path)}]
        cleaned_conversations = []
        client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"sha": "abc"}, request=request)))

        def profile(self, *_):
            return "user"

        @contextmanager
        def conversation(self, *_):
            yield 1

    monkeypatch.setattr(online_baseline.golden, "run_case", lambda *_, **__: {"rounds": [
        {"error": None, "request_evidence": {"request_id": "captured-request"}}]})
    monkeypatch.setattr(online_baseline.golden, "check_case", lambda *_: [])
    case = {"id": "gallery-followup", "identity": "user", "tags": ["regression"]}
    adapter = Fake()
    try:
        report = online_baseline.run([case], adapter, {"user": 7}, tmp_path / "report",
                                     phase="poc", suite_hash="hash")
    finally:
        adapter.client.close()
    task, = report["task_metrics"]["tasks"]
    assert task["case_ids"] == ["gallery-followup"]
    assert task["calls"] == 1 and task["cache_hit_rate"] == 0.8
    assert task["provider_cost_usd"] is None
    assert not report["full_run"] and report["phase"] == "poc"
