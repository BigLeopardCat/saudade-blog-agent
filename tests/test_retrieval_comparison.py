"""Three-way comparisons retain misses, incomplete evidence and unjudged boundaries."""

import copy
from pathlib import Path

import pytest

from eval.offline_rag_eval import evaluate
from eval.rag_dataset import load
from eval.retrieval_comparison import analyze, failure_stage


def test_paired_comparison_does_not_drop_failed_or_unanswerable_queries():
    data = load(Path(__file__).resolve().parents[1] / "eval/fixtures/offline_smoke", "test")
    before = {"single": {}, "multi": {"cursor": 1}, "absent": {"noise": 1}}
    after = {"single": {"cursor": 1}, "multi": {"cursor": 2, "hash": 1}, "absent": {}}
    report = {"input_hashes": data["hashes"], "config": {"cutoffs": [3]},
              "phase_seconds": {qid: {"lexical": 0.01, "vector": 0.1, "fusion": 0.001}
                                for qid in data["queries"]},
              "arms": {"bm25": evaluate(data, before, [3]),
                       "vector": evaluate(data, after, [3]),
                       "hybrid": evaluate(data, after, [3])}}
    result = analyze(report, data, 3)
    assert result["failure_stage_counts"]["bm25"] == {
        "no_relevant_document_in_top_k": 1, "incomplete_document_evidence": 1,
        "unanswerable_candidate_observation": 1}
    change = result["paired_comparisons"]["bm25_to_vector"]["Success@3"]
    assert change["pairs"] == 2 and change["wins"] == 1 and change["losses"] == 0
    assert change["outcomes"]["exact_mcnemar_p"] == 1
    assert result["semantic_judge_calls"] == 0
    changed = copy.deepcopy(report)
    changed["input_hashes"]["manifest.json"] = "different"
    with pytest.raises(ValueError, match="hashes"):
        analyze(changed, data, 3)


def test_document_hits_do_not_prove_passage_coverage_or_generation_correctness():
    query = {"evidence_spans": [[{}]]}
    assert failure_stage(query, {"a": 1}, {"Success@8": 1}, 8) == (
        "passage_locations_unverified")
    assert failure_stage(query, {"a": 1}, {
        "Success@8": 1, "PassageEvidenceCoverage@8": 0.5}, 8) == (
        "document_hit_but_passage_evidence_incomplete")
