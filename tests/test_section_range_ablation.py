"""Section expansion changes evidence volume, never document or section selection."""

import copy

import pytest

from eval.offline_rag_eval import evaluate
from eval.section_range_ablation import compare, expand, volume
from rag.sections import outline


def test_section_expansion_covers_existing_section_without_selecting_an_unreturned_one():
    text = "## 已选\n前文。答案。\n## 未选\n另一个答案。"
    selected, other = outline(text)
    did = "doc"
    queries = {"q": {"evidence_spans": [{"document_id": did,
        "section_id": selected["section_id"], "start": 3, "end": 5}]},
        "other": {"evidence_spans": [{"document_id": did,
            "section_id": other["section_id"], "start": 0, "end": 2}]}}
    data = {"corpus": {did: {"text": text}}, "queries": queries,
            "qrels": {qid: {did: 1} for qid in queries}, "hashes": {"source": "fixed"}}
    run = {qid: {did: 1.} for qid in queries}
    locations = {qid: [{"document_id": did, "matches": [{
        "section_id": selected["section_id"], "offset": 0, "end": 2}]}] for qid in queries}
    report = {"input_hashes": data["hashes"], "config": {"cutoffs": [8]},
              "arms": {"vector": {**evaluate(data, run, [8], locations=locations),
                                    "locations": locations}}}
    untouched = copy.deepcopy(report)
    result = compare(data, report)
    assert report == untouched
    arm = result["arms"]["vector"]
    assert arm["recovered_query_ids"] == ["q"]
    assert arm["same_returned_sections_expanded"]["all"][
        "PassageEvidenceCoverage@8"]["value"] == .5
    assert arm["expanded_mean_fetchable_chars"] > arm["original_mean_fetchable_chars"]
    assert result["model_calls"] == result["database_calls"] == 0
    with pytest.raises(ValueError, match="source binding"):
        compare({**data, "hashes": {"source": "changed"}}, report)


def test_range_volume_counts_overlap_once_and_rejects_invalid_source_ranges():
    matches = [{"section_id": "s", "offset": a, "end": b}
               for a, b in [(0, 4), (2, 6), (8, 9)]]
    hits = [{"document_id": "doc", "matches": matches}]
    assert volume(hits) == 7
    assert expand({"q": hits}, {"doc": {"s": "x" * 9}})["q"][0]["matches"] == [
        {"section_id": "s", "offset": 0, "end": 9}]
    with pytest.raises(ValueError, match="outside pinned section"):
        expand({"q": hits}, {"doc": {"s": "x" * 8}})
