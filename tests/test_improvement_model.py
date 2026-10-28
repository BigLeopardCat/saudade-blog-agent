"""Candidate generation uses one bounded call and preserves rejected output evidence."""

import json
from unittest.mock import Mock

import httpx
import pytest
from _improvement_support import case, setup
from langchain_core.messages import AIMessage

from eval.improvement import candidates
from eval.online_baseline import run
from eval.online_transport import EvaluationError


@pytest.mark.parametrize("valid", [True, False])
def test_generation_never_retries_or_applies_code_and_records_usage(tmp_path, monkeypatch, valid):
    registry, _, packet, *_ = setup(tmp_path)
    response = {"candidates": [{"title": "Address", "rationale": "Verified caller", "edits": [{
        "path": candidates.TARGETS[0], "old": "'before'", "new": "'verified'"}]}]}
    result = AIMessage(content=json.dumps(response) if valid else "invalid JSON",
        usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})
    model = Mock()
    model.bind.return_value = model
    model.invoke.return_value = result
    factory = Mock(return_value=model)
    monkeypatch.setattr("models.llm.get_llm", factory)
    if valid:
        key = candidates.generate(registry, packet)
        assert registry.get(key, "proposals")["usage"]["input_tokens"] == 100
    else:
        with pytest.raises(ValueError, match="generation evidence"):
            candidates.generate(registry, packet)
    assert model.invoke.call_count == 1
    assert factory.call_args.kwargs["max_retries"] == 0
    assert factory.call_args.kwargs["max_tokens"] == 2048
    artifacts = [json.loads(path.read_bytes())
                 for path in (registry.root / "artifacts").glob("*.json")]
    attempts = [row for row in artifacts if row["kind"] == "generation_attempt"]
    assert len(attempts) == 1 and attempts[0]["payload"]["usage"]["output_tokens"] == 20


def test_wrong_deployed_build_stops_before_profile_or_model_calls(tmp_path):
    adapter = Mock()
    adapter.agent_url = "http://agent"
    adapter.client.get.return_value = httpx.Response(200, json={"sha": "other"},
        request=httpx.Request("GET", "http://agent/version"))
    with pytest.raises(EvaluationError, match="frozen candidate"):
        run([case("dev")], adapter, {"user": 7}, tmp_path / "run", phase="full",
            suite_hash="suite", expected_build="candidate")
    adapter.profile.assert_not_called()
    assert adapter.client.get.call_count == 1
