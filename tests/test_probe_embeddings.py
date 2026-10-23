"""Measure the real SDK path without network; reject malformed/failed responses."""

import json

import httpx
import pytest
from openai import OpenAI

from eval.probe_embeddings import probe


def client(handler):
    return OpenAI(
        base_url="https://embedding.invalid/v1",
        api_key="test-secret",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def response(vector):
    return {
        "object": "list",
        "model": "test",
        "usage": {"prompt_tokens": 7, "total_tokens": 7},
        "data": [{"object": "embedding", "index": 0, "embedding": vector}],
    }


def test_probe_preserves_exact_inputs_and_records_each_api_failure():
    received = []

    def handler(request):
        received.append(json.loads(request.content))
        if len(received) == 1:
            return httpx.Response(200, json=response([0.6, 0.8]))
        return httpx.Response(429, json={"error": {"message": "test-secret"}})

    cases = [{"_id": "short", "text": "完整中文输入"}, {"_id": "long", "text": "长" * 1800}]
    with client(handler) as sdk:
        rows = probe(sdk, "test", cases, 1, 2, None)
    assert received[0]["input"] == cases[0]["text"]
    assert received[1]["input"] == cases[1]["text"]
    assert len(received) == 2  # A 429 must not trigger hidden retries or disappear from the report.
    assert rows[0]["ok"] and rows[0]["norm"] == 1 and rows[0]["prompt_tokens"] == 7
    assert not rows[1]["ok"] and rows[1]["status"] == 429
    assert "test-secret" not in json.dumps(rows)


@pytest.mark.parametrize("vector", [[1.0], [0.0, 0.0], [float("nan"), 1.0]])
def test_probe_rejects_wrong_dimension_zero_and_nonfinite_vectors(vector):
    def handler(request):
        # HTTP response fixture needs raw JSON to represent a deliberately invalid NaN.
        return httpx.Response(
            200, content=json.dumps(response(vector)), headers={"content-type": "application/json"}
        )

    with client(handler) as sdk, pytest.raises(ValueError, match="invalid embedding"):
        probe(sdk, "test", [{"_id": "q", "text": "query"}], 1, 2, None)
