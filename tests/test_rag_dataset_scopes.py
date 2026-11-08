"""Public development and evaluation data remain explicit and reproducible offline."""

import json
from pathlib import Path

import pytest

from eval.offline_rag_eval import bm25_run, evaluate
from eval.rag_dataset import load
from rag import DEFAULT_TOP_K

DATASET = Path(__file__).resolve().parents[1] / "eval/fixtures/blog_retrieval"


def test_splits_are_disjoint_and_every_authored_question_is_accounted_for():
    development, evaluation = (load(DATASET, split) for split in ("dev", "test"))
    assert not set(development["queries"]) & set(evaluation["queries"])
    all_rows = [json.loads(line) for line in (DATASET / "queries.jsonl").read_text().splitlines()]
    assert set(development["queries"]) | set(evaluation["queries"]) == {
        row["_id"] for row in all_rows}
    assert development["corpus"] == evaluation["corpus"]
    assert "not a production quality score" in development["manifest"]["scope"]
    assert "not a secret" in evaluation["manifest"]["splits"]["test"]
    assert {row["category"] for row in evaluation["queries"].values()} >= {
        "multi_document", "unanswerable", "pagination", "similar_terms", "failure_semantics"}
    assert len(development["corpus"]) == 28
    assert len(development["queries"]) == 14 and len(evaluation["queries"]) == 21


@pytest.mark.parametrize("split", ["dev", "test"])
def test_standard_run_retains_every_question_and_their_actual_metric_denominators(split):
    data = load(DATASET, split)
    run, _ = bm25_run(data, DEFAULT_TOP_K)
    report = evaluate(data, run, [1, 3, DEFAULT_TOP_K])
    assert set(report["per_query"]) == set(data["queries"]) == set(run)
    positives = sum(any(grade > 0 for grade in rows.values()) for rows in data["qrels"].values())
    assert report["summary"]["all"][f"R@{DEFAULT_TOP_K}"]["queries"] == positives
    for qid, row in data["queries"].items():
        if row.get("answerable") is False:
            assert f"R@{DEFAULT_TOP_K}" not in report["per_query"][qid]
            assert "CandidateReturnedOnUnanswerable" in report["per_query"][qid]
        if row.get("evidence_groups"):
            assert f"EvidenceCoverage@{DEFAULT_TOP_K}" in report["per_query"][qid]
