"""Production retrieval defaults and complete evaluation through the actual embedding SDK."""

import json
from pathlib import Path

import httpx
import pytest
from _rag_store import memory_store, memory_vectors  # noqa: F401
from openai import OpenAI
from test_embedding_inputs import profile as profile

from config import settings
from eval import model_retrieval_run as runner
from eval.rag_dataset import load
from rag import DEFAULT_TOP_K, chunks, vector_index

FIXTURE = Path(__file__).resolve().parents[1] / "eval/fixtures/offline_smoke"


@pytest.fixture
def endpoint(monkeypatch, tmp_path):
    for name, value in {
        "embedding_model": "fixture", "embedding_api_key": "test-only",
        "embedding_base_url": "https://embedding.fixture.test/v1", "embedding_profile": "",
        "embedding_dim": 2, "rag_hybrid_enabled": True,
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(vector_index, "_store", None)
    monkeypatch.setenv("SAUDADE_RAG_EVAL_DSN", settings.rag_vector_dsn)
    monkeypatch.setattr(runner, "VectorStore",
                        lambda repository: memory_store(repository.collection))
    monkeypatch.setattr(vector_index, "_query_cache", {})
    monkeypatch.setattr(vector_index, "_query_order", [])
    monkeypatch.setattr(chunks, "MAX_CHARS", chunks.MAX_CHARS)
    requests = []

    def respond(request):
        assert request.url.path == "/v1/embeddings"
        body = json.loads(request.content)
        requests.append(body)
        if "synthetic-outage" in body["input"]:
            return httpx.Response(503, json={"error": {"message": "synthetic outage"}})
        return httpx.Response(200, json={"model": "fixture", "data": [
            {"index": index, "embedding": [1.0, 0.0]}
            for index, _ in enumerate(body["input"])]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        sdk = OpenAI(api_key="test-only", base_url=settings.embedding_base_url,
                     http_client=client, max_retries=0)
        monkeypatch.setattr(vector_index, "_get_client", lambda: sdk)
        yield requests


def arguments(tmp_path, *extra):
    return [str(FIXTURE), "--split", "test", "--base-url",
            "https://embedding.fixture.test/v1", "--model", "fixture", "--expected-dim", "2",
            "--collection", "eval-" + tmp_path.name, "--output", str(tmp_path / "report.json"),
            *extra]


def test_cli_defaults_share_production_cutoffs_and_record_the_complete_config(endpoint, tmp_path):
    production_chars = chunks.MAX_CHARS
    assert runner.main(arguments(tmp_path)) == 0
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["configuration_mode"] == "production_retrieval_defaults"
    assert report["config"]["chunk_chars"] == production_chars
    assert report["config"]["top_k"] == report["config"]["candidates"] == DEFAULT_TOP_K
    assert report["config"]["cutoffs"] == [1, 3, DEFAULT_TOP_K]
    assert len(report["config_sha256"]) == len(report["code_sha256"]) == 64
    assert set(report["arms"]) == {"bm25", "vector", "hybrid"}
    assert report["query_cache_initial_entries"] == 0
    assert set(report["input_hashes"]) == {
        "manifest.json", "corpus.jsonl", "queries.jsonl", "qrels/test.tsv"}
    for qid, duration in report["query_seconds"].items():
        assert duration == pytest.approx(sum(report["phase_seconds"][qid].values()))
    assert all(len(hits) <= DEFAULT_TOP_K for arm in report["arms"].values()
               for hits in arm["run"].values())
    assert endpoint


def test_research_pool_and_chunk_changes_are_explicit_and_not_output_limits(endpoint, tmp_path):
    assert runner.main(arguments(tmp_path, "--chunk-chars", "128", "--candidates", "5",
                                 "--top-k", "2", "--k", "1", "2")) == 0
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["configuration_mode"] == "research_override"
    assert report["config"]["chunk_chars"] == 128
    assert report["config"]["candidates"] == 5 and report["config"]["top_k"] == 2
    assert all(len(hits) <= 2 for arm in report["arms"].values() for hits in arm["run"].values())


@pytest.mark.parametrize("extra", [
    ["--candidates", "7"], ["--top-k", "0"], ["--k", "10"], ["--k", "0"],
])
def test_invalid_windows_fail_before_any_embedding_request(endpoint, tmp_path, extra):
    with pytest.raises(SystemExit, match="2"):
        runner.main(arguments(tmp_path, *extra))
    assert not endpoint and not (tmp_path / "report.json").exists()


def test_profile_refinement_matches_production_and_reuses_unchanged_vectors(
    endpoint, tmp_path, request
):
    request.getfixturevalue("profile")
    data = {"corpus": {"doc": {"title": "短标题", "text": "## 范围\n" + "正文" * 100}},
            "queries": {"query": {"text": "正文"}}, "qrels": {"query": {"doc": 1}},
            "manifest": {"scope": "synthetic"}, "hashes": {}}
    report = runner.execute(data, tmp_path / "profile-index", DEFAULT_TOP_K, 2)
    assert report["chunks"] > report["lexical_chunks"]
    first_requests = len(endpoint)
    repeated = runner.execute(data, tmp_path / "profile-index", DEFAULT_TOP_K, 2)
    assert repeated["index_update"]["skipped"] == "aligned"
    assert len(endpoint) == first_requests
    assert repeated["config_sha256"] == report["config_sha256"]


def test_degraded_embedding_cannot_be_published_as_hybrid_quality(endpoint, tmp_path):
    data = load(FIXTURE, "test")
    data["queries"]["single"]["text"] = "synthetic-outage"
    with pytest.raises(ValueError, match="query retrieval failed"):
        runner.execute(data, tmp_path / "outage-index", DEFAULT_TOP_K, 2)
