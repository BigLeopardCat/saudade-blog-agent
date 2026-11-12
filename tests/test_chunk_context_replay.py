"""Frozen response lengths can verify estimated costs without redistributing public source text."""

import copy
import hashlib
import json
import lzma
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval.chunk_context_comparison import compare, hybrid_locations
from eval.replay_chunk_context import replay


def fixture():
    data = {"manifest": {}, "hashes": {"corpus.jsonl": "fixed"},
            "corpus": {"a": {"text": "证据。前后上下文。"}},
            "queries": {"q": {"evidence_spans": [{"document_id": "a", "section_id": "s1",
                                                  "start": 0, "end": 3}]}},
            "qrels": {"q": {"a": 1}}}
    positions = [{"document_id": "a", "matches": [{"section_id": "s1", "offset": 4, "end": 6}]}]
    merged = hybrid_locations(data, positions, positions, ["a"])
    assert merged == positions
    arm = {"run": {"q": {"a": 1.}}, "locations": {"q": positions}}
    views = {name: arm for name in ["bm25", "old_vector", "new_vector",
                                   "old_hybrid24", "new_hybrid24"]}
    result = compare(data, views, SimpleNamespace(count=len), groups={"fixed": ["q"]})
    return {"du": result, "cmrc": copy.deepcopy(result)}


def test_frozen_context_metrics_and_costs_replay_without_source_bodies():
    result = replay(fixture())
    assert result["datasets"]["du"] == {"queries": 1, "ranking_arms": 5, "reading_policies": 15}
    assert result["model_calls"] == result["database_calls"] == 0


def test_observed_frame_length_corruption_is_detected():
    report = fixture()
    report["du"]["contexts"]["old_vector/parent"]["per_query_frame_lengths"]["q"][0] += 100
    with pytest.raises(ValueError, match="frozen metric mismatch"):
        replay(report)


def test_published_chunk_context_metrics_replay_without_sources_or_models():
    directory = Path(__file__).resolve().parents[1] / "eval/report"
    ledger = json.loads((directory / "baseline_chunk_context_operations_20261112.json").read_text())
    for name, expected in ledger["published_artifacts"].items():
        value = (directory / name).read_bytes()
        assert len(value) == expected["bytes"]
        assert hashlib.sha256(value).hexdigest() == expected["sha256"]
    with lzma.open(directory / "baseline_chunk_context_20261112.json.xz", "rt") as stream:
        result = replay(json.load(stream))
    assert result["datasets"] == {
        "du": {"queries": 2000, "ranking_arms": 5, "reading_policies": 15},
        "cmrc": {"queries": 3219, "ranking_arms": 5, "reading_policies": 15}}
    assert result["model_calls"] == result["database_calls"] == 0
