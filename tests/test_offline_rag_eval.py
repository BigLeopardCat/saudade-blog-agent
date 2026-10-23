"""Offline evaluation must measure production retrieval without external IO."""

import copy
import json
from pathlib import Path

import pytest

from eval.offline_rag_eval import bm25_run, evaluate, main, read_run
from eval.rag_dataset import load

FIXTURE = Path(__file__).resolve().parents[1] / "eval/fixtures/offline_smoke"


def dataset():
    return load(FIXTURE, "test")


def test_hit_does_not_masquerade_as_recall_or_complete_multi_evidence():
    data = dataset()
    run = {"single": {"cursor": 2}, "multi": {"cursor": 2}, "absent": {}}
    result = evaluate(data, run, [1, 3])
    row = result["per_query"]["multi"]
    assert row["Success@1"] == 1
    assert row["R@1"] == 0.5
    assert row["EvidenceCoverage@1"] == 0.5
    run["multi"]["hash"] = 1
    complete = evaluate(data, run, [1, 3])["per_query"]["multi"]
    assert complete["EvidenceCoverage@3"] == complete["R@3"] == 1
    assert result["summary"]["all"]["Success@1"]["queries"] == 2
    assert "R@1" not in result["per_query"]["absent"]


def test_missing_hits_reduce_metrics_and_noise_is_not_a_generation_judgment():
    run = {"single": {}, "multi": {}, "absent": {"noise": 1}}
    result = evaluate(dataset(), run, [3])
    assert result["summary"]["all"]["R@3"]["value"] == 0
    assert result["per_query"]["absent"]["CandidateReturnedOnUnanswerable"] == 1


def test_alternative_evidence_does_not_require_every_equivalent_document():
    data = dataset()
    data["queries"]["multi"]["evidence_groups"] = [["cursor", "hash"]]
    result = evaluate(data, {"single": {}, "multi": {"hash": 1}, "absent": {}}, [1])
    assert result["per_query"]["multi"]["EvidenceCoverage@1"] == 1


def test_production_bm25_is_offline_even_with_hybrid_enabled(monkeypatch):
    import rag.search as search
    from config import settings

    def forbidden(*args, **kwargs):
        pytest.fail("offline evaluation must not fetch corpus or warm vectors")

    monkeypatch.setattr(settings, "rag_hybrid_enabled", True)
    monkeypatch.setattr(search, "warm_vectors", forbidden)
    monkeypatch.setattr(search.RagIndex, "_fetch_corpus", forbidden)
    run, timing = bm25_run(dataset(), 3, True)
    assert list(run["single"])[0] == "cursor"
    assert set(run["multi"]) == {"cursor", "hash"}
    assert run["absent"] == {}
    assert timing["build_seconds"] >= 0


def test_cli_writes_hashes_and_per_query_results(tmp_path):
    output = tmp_path / "report.json"
    assert main([str(FIXTURE), "--output", str(output)]) == 0
    result = json.loads(output.read_text())
    assert result["engine"] == "production-bm25"
    assert len(result["code_hash"]) == 64
    assert len(result["input_hashes"]) == 4
    assert result["dataset"]["scope"].startswith("smoke only")
    assert result["per_query"]["multi"]["EvidenceCoverage@3"] == 1


@pytest.mark.parametrize(
    "line",
    [
        "single Q0 missing 1 1 test\n",
        "unknown Q0 cursor 1 1 test\n",
        "single Q0 cursor 1 nan test\n",
        "single Q0 cursor 1 1 t\nsingle Q0 cursor 2 0 t\n",
    ],
)
def test_invalid_external_runs_are_rejected(tmp_path, line):
    path = tmp_path / "run.txt"
    path.write_text(line)
    with pytest.raises(ValueError):
        read_run(path, dataset())


def test_empty_external_query_is_retained_and_trec_results_match(tmp_path):
    path = tmp_path / "run.txt"
    path.write_text("multi Q0 hash 1 2 test\nmulti Q0 cursor 2 1 test\n")
    run = read_run(path, dataset())
    assert run["single"] == run["absent"] == {}
    assert evaluate(dataset(), run, [3])["per_query"]["multi"]["R@3"] == 1


@pytest.mark.parametrize("fault", ["duplicate", "unknown_qrel", "missing_label", "bad_group"])
def test_dataset_validation_is_fail_closed(tmp_path, fault):
    import shutil

    root = tmp_path / "data"
    shutil.copytree(FIXTURE, root)
    if fault == "duplicate":
        path = root / "corpus.jsonl"
        path.write_text(path.read_text() + path.read_text().splitlines()[0] + "\n")
    elif fault == "unknown_qrel":
        with (root / "qrels/test.tsv").open("a") as stream:
            stream.write("single\tunknown\t1\n")
    else:
        queries = [json.loads(line) for line in (root / "queries.jsonl").read_text().splitlines()]
        if fault == "missing_label":
            queries[-1].pop("answerable")
        else:
            queries[1]["evidence_groups"] = [["noise"]]
        (root / "queries.jsonl").write_text("\n".join(json.dumps(q) for q in queries))
    with pytest.raises(ValueError):
        load(root, "test")


def test_limits_and_content_identity_are_not_silent_subsampling(tmp_path):
    import shutil

    with pytest.raises(ValueError, match="limit"):
        load(FIXTURE, "test", max_docs=1)
    with pytest.raises(ValueError, match="split"):
        load(FIXTURE, "../test")
    shutil.copytree(FIXTURE, tmp_path / "data")
    path = tmp_path / "data/corpus.jsonl"
    path.write_text(path.read_text().replace("baking bread", "baking pizza"))
    changed = load(tmp_path / "data", "test")
    assert changed["hashes"]["corpus.jsonl"] != dataset()["hashes"]["corpus.jsonl"]
    assert copy.deepcopy(changed)["manifest"] == dataset()["manifest"]
