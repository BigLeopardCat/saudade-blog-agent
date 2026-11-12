"""Same-pool reranking must retain query text, source offsets and checkpoint bindings."""

import gzip
import json
from pathlib import Path

import pytest
from tokenizers import Tokenizer, models, pre_tokenizers, processors

from eval.reranker_inputs import PairEncoder, candidate_hits
from eval.reranker_run import input_digest, run


def encoder(tmp_path):
    unknown = "[UNK]"
    tokenizer = Tokenizer(models.WordLevel(
        {"[CLS]": 0, "[PAD]": 1, "[SEP]": 2, "[UNK]": 3, "x": 4, "q": 5},
        unk_token=unknown))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]", pair="[CLS] $A [SEP] $B:1 [SEP]:1",
        special_tokens=[("[CLS]", 0), ("[SEP]", 2)])
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    return PairEncoder(path)


def test_pair_budget_keeps_complete_query_and_records_clipped_source(tmp_path):
    pair = encoder(tmp_path).encode("q q", "x", "x " * 700,
                                    {"section_id": "s2", "offset": 100, "end": 1500})
    assert len(pair["input_ids"]) == 512
    assert pair["input_ids"][1:3] == [5, 5]
    assert pair["query_tokens"] == 2 and pair["truncated"]
    assert pair["visible_source"][0]["offset"] == 100
    assert 100 < pair["visible_source"][0]["end"] < 1500
    assert pair["visible_source_chars"] < pair["anchor_chars"]
    with pytest.raises(ValueError, match="query alone"):
        encoder(tmp_path).encode("q " * 512, "", "x", {"section_id": "s1", "offset": 0})


def test_candidate_fusion_preserves_fixed_union_and_rejects_missing_locator():
    data = {"hashes": {"corpus.jsonl": "fixed"}, "corpus": {
        "a": {"text": "甲"}, "b": {"text": "乙"}}}
    left = [{"document_id": "a", "matches": [{"section_id": "s1", "offset": 0, "end": 1}]}]
    right = [{"document_id": "b", "matches": [{"section_id": "s1", "offset": 0, "end": 1}]}]
    assert [r["id"] for r in candidate_hits(data, left, right, ["a", "b"])] == ["a", "b"]
    with pytest.raises(ValueError, match="identities/order"):
        candidate_hits(data, left, right, ["a"])
    with pytest.raises(ValueError, match="no source locator"):
        candidate_hits(data, [{"document_id": "a", "matches": []}], [], ["a"])


def test_checkpoint_replay_avoids_recomputation_and_rejects_changed_inputs(tmp_path):
    calls = []

    class Model:
        def score(self, pairs):
            calls.append(len(pairs))
            return [float(p["input_ids"][0]) for p in pairs], len(pairs)

    row = {"query_id": "q", "candidates": [
        {"document_id": "a", "input_ids": [4], "input_tokens": 1},
        {"document_id": "b", "input_ids": [5], "input_tokens": 1}]}
    source, output = tmp_path / "inputs.gz", tmp_path / "scores.jsonl"
    with gzip.open(source, "wt") as stream:
        stream.write(json.dumps(row) + "\n")
    assert run(Model(), source, output)["new_queries"] == 1
    saved = json.loads(output.read_text())
    assert saved["input_sha256"] == input_digest(row)
    assert saved["scores"][1]["logit"] == 5.
    assert run(Model(), source, output)["new_queries"] == 0
    assert calls == [2]
    row["candidates"][0]["input_ids"] = [6]
    with gzip.open(source, "wt") as stream:
        stream.write(json.dumps(row) + "\n")
    with pytest.raises(ValueError, match="checkpoint input"):
        run(Model(), source, output)


def frozen_fixture(tmp_path):
    from types import SimpleNamespace

    from eval.reranker_analysis import decision, quantiles, score_views, source_views, stages

    data = {"manifest": {}, "hashes": {"corpus.jsonl": "fixed"},
            "corpus": {"a": {"text": "证据甲。"}, "b": {"text": "证据乙。"}},
            "queries": {"q": {}}, "qrels": {"q": {"a": 1}}}
    candidates = [{"document_id": d, "input_ids": [4, 5], "input_tokens": 2,
        "full_pair_tokens": 3, "query_tokens": 1, "truncated": True,
        "anchor": {"section_id": "s1", "offset": 0, "end": 4}, "anchor_chars": 4,
        "visible_source": [{"section_id": "s1", "offset": 0, "end": 1}],
        "visible_source_chars": 1,
        "retrieval_matches": [{"section_id": "s1", "offset": 0, "end": 4}]} for d in ["a", "b"]]
    row = {"query_id": "q", "candidates": candidates}
    source = tmp_path / "pairs.gz"
    with gzip.open(source, "wt") as stream:
        stream.write(json.dumps(row) + "\n")
    scores = {"q": {"query_id": "q", "input_sha256": input_digest(row),
        "scores": [{"document_id": d, "logit": 1.} for d in ["a", "b"]],
        "seconds": .01, "cpu_seconds": .01, "padded_tokens": 4, "unpadded_tokens": 4,
        "model_batches": 1}}
    ranked = {"bm25": {"q": {"a": 1., "b": 0.}},
              "vector_reference": {"q": {"b": 1., "a": 0.}}}
    views, audits, timings, routes = source_views(source, scores, ranked)
    assert views["reranker"]["locations"]["q"][0]["matches"][0]["end"] == 4
    assert list(views["reranker"]["run"]["q"]) == ["a", "b"]
    groups = {"has_positive_source_over_1800_chars": ["q"]}
    docs, contexts, cohorts, _ = score_views(data, views, SimpleNamespace(count=len), groups)
    performance = {k: quantiles([v[k] for v in timings.values()]) for k in timings["q"]}
    plan = json.loads((Path(__file__).resolve().parents[1] /
        "eval/report/baseline_reranker_plan_20261112.json").read_text())
    report = {"dataset": {}, "input_hashes": data["hashes"],
              "evaluation_labels": {"queries": data["queries"], "qrels": data["qrels"]},
              "documents": docs, "contexts": contexts, "fixed_group_query_ids": groups,
              "groups": cohorts, "input_audits": audits, "query_performance": timings,
              "performance": performance, "candidate_routes": routes,
              "candidate_scores": {q: v["scores"] for q, v in scores.items()}, "plan": plan,
              "model_runtime_binding": {"batch_size": 2},
              "failure_stages": stages(data, views, audits)}
    report["decision"] = decision(docs, cohorts, performance, plan)
    return report


def test_frozen_reranker_replay_keeps_original_read_ranges_and_stable_ties(tmp_path):
    from eval.replay_reranker import replay

    report = frozen_fixture(tmp_path)
    assert replay(report)["candidate_pairs"] == 2
    assert report["decision"]["checks"]["added_warm_p95"]
    assert not report["decision"]["checks"]["ndcg_gain_over_both"]


def test_frozen_replay_rejects_scores_that_change_the_rank(tmp_path):
    from eval.replay_reranker import replay

    report = frozen_fixture(tmp_path)
    report["candidate_scores"]["q"][1]["logit"] = 2.
    with pytest.raises(ValueError, match="score/rank mismatch"):
        replay(report)


def test_scoring_input_missing_answer_is_separate_from_agent_read_range(tmp_path):
    from eval.replay_reranker import replay_input_audit
    from eval.reranker_input_audit import audit

    text = "x " * 700 + "q"
    data = {"manifest": {}, "hashes": {"corpus.jsonl": "fixed"},
            "corpus": {"a": {"text": text}}, "qrels": {"q": {"a": 1}},
            "queries": {"q": {"text": "q", "evidence_spans": [{"document_id": "a",
                "section_id": "s1", "start": 1400, "end": 1401, "quote": "q"}]}}}
    hits = {"q": [{"document_id": "a", "matches": [
        {"section_id": "s1", "offset": 0, "end": len(text)}]}]}
    before = {"input_hashes": data["hashes"], "arms": {"hybrid": {
        "run": {"q": {"a": 1.}}, "locations": hits}}}
    report = audit(data, before, encoder(tmp_path))
    assert report["original"]["per_query"]["q"]["PassageEvidenceCoverage@8"] == 1
    assert report["reranker_input"]["per_query"]["q"]["PassageEvidenceCoverage@8"] == 0
    assert report["original"]["locations"]["q"][0]["matches"][0]["end"] == len(text)
    assert report["model_calls"] == 0 and replay_input_audit(report) == 1


def test_frozen_locators_rebuild_exact_model_inputs_without_embedding(tmp_path):
    from eval.reranker_inputs import rebuild

    text = "x " * 700
    data = {"hashes": {"corpus.jsonl": "fixed"}, "corpus": {"a": {"text": text}},
            "queries": {"q": {"text": "q"}}}
    codec = encoder(tmp_path)
    match = {"section_id": "s1", "offset": 0, "end": len(text)}
    pair = {"document_id": "a", **codec.encode("q", "", text, match),
            "retrieval_matches": [match]}
    row = {"query_id": "q", "candidates": [pair]}
    report = {"input_hashes": data["hashes"], "input_digests": {"q": input_digest(row)},
              "input_audits": {"q": [{k: v for k, v in pair.items() if k != "input_ids"}]}}
    output = tmp_path / "reconstructed.gz"
    assert rebuild(data, report, codec, output)["candidate_pairs"] == 1
    with gzip.open(output, "rt") as stream:
        assert input_digest(json.loads(next(stream))) == report["input_digests"]["q"]
    report["input_digests"]["q"] = "changed"
    with pytest.raises(ValueError, match="model input digest changed"):
        rebuild(data, report, codec, output)
