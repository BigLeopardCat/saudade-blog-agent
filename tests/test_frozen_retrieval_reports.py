"""Frozen artifacts can be verified and scored without any model or database access."""

import hashlib
import json
from pathlib import Path

import pytest

from eval.offline_rag_eval import evaluate
from eval.rag_dataset import load

ROOT = Path(__file__).resolve().parents[1] / "eval"


def test_archived_experiments_preserve_the_original_artifact_bytes():
    archive = ROOT / "report" / "archive"
    manifest = json.loads((archive / "manifest.json").read_text())
    paths = set()
    for item in manifest["artifacts"]:
        relative = Path(item["path"])
        assert relative.parts[:3] == ("eval", "report", "archive")
        assert relative.name not in paths
        paths.add(relative.name)
        assert hashlib.sha256((archive / relative.name).read_bytes()).hexdigest() == item["sha256"]
        assert not (ROOT.parent / item["original_path"]).exists()
    assert paths == {path.name for path in archive.glob("baseline_*.json")}


def test_frozen_artifact_hashes_match_the_operation_ledger():
    root = ROOT / "report"
    ledger = json.loads((root / "baseline_rag_benchmark_operations_20261112.json").read_text())
    for name, expected in ledger["artifact_sha256"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected
    assert ledger["metric_replay"]["model_calls"] == 0
    assert ledger["embedding_runtime"]["semantic_judge_calls"] == 0


@pytest.mark.parametrize("split", ["dev", "test"])
def test_blog_frozen_runs_reproduce_each_query_and_summary_without_embedding(split):
    report = json.loads((ROOT / "report" / (
        f"baseline_blog_evidence_{split}_20261112.json")).read_text())
    data = load(ROOT / "fixtures/blog_evidence", split)
    assert report["input_hashes"] == data["hashes"]
    for arm in report["arms"].values():
        replay = evaluate(data, arm["run"], report["config"]["cutoffs"],
                          locations=arm["locations"])
        assert replay["per_query"].keys() == arm["per_query"].keys()
        for qid, metrics in replay["per_query"].items():
            assert metrics == pytest.approx(arm["per_query"][qid], abs=1e-12)
        for bucket, metrics in replay["summary"].items():
            assert metrics.keys() == arm["summary"][bucket].keys()
            for metric, value in metrics.items():
                expected = arm["summary"][bucket][metric]
                assert value["queries"] == expected["queries"]
                assert value["value"] == pytest.approx(expected["value"], abs=1e-12)
