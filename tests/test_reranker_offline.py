"""Same-pool reranking must retain query text, source offsets and checkpoint bindings."""

import gzip
import json

import pytest
from tokenizers import Tokenizer, models, pre_tokenizers, processors

from eval.reranker_inputs import PairEncoder, candidate_hits
from eval.reranker_run import input_digest, run


def encoder(tmp_path):
    tokenizer = Tokenizer(models.WordLevel(
        {"[CLS]": 0, "[PAD]": 1, "[SEP]": 2, "[UNK]": 3, "x": 4, "q": 5},
        unk_token="[UNK]"))
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
