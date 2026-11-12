"""Only source ranges visible after actual frame budgeting count as retrieved evidence."""

from types import SimpleNamespace

from eval.context_retrieval_eval import read_contexts, read_frame
from rag.context_windows import merge_read_windows

PROFILE = SimpleNamespace(count=len)


def fixture():
    body = "前面的证据。" + "解释" * 20 + "检索锚点。" + "后续说明" * 20
    start = body.index("检索锚点")
    data = {"corpus": {"d": {"title": "T", "text": body}},
            "queries": {"q": {"evidence_spans": [{"document_id": "d", "section_id": "s1",
                                                  "start": 0, "end": 6}]}},
            "qrels": {"q": {"d": 1}}}
    arm = {"run": {"q": {"d": 1.}}, "locations": {"q": [{"document_id": "d", "matches": [
        {"section_id": "s1", "offset": start, "end": start + 4}]}]}}
    return data, arm


def test_parent_read_recovers_evidence_while_doc_rank_stays_fixed_and_cost_is_counted():
    data, arm = fixture()
    window = read_contexts(data, arm, PROFILE)
    parent = read_contexts(data, arm, PROFILE, mode="parent")
    assert window["per_query"]["q"]["PassageEvidenceCoverage@8"] == 0
    assert parent["per_query"]["q"]["PassageEvidenceCoverage@8"] == 1
    assert window["run"] == parent["run"] == arm["run"]
    assert parent["per_query_cost"]["q"]["frame_estimated_tokens"] > (
        window["per_query_cost"]["q"]["frame_estimated_tokens"])
    assert parent["http_calls"] == parent["model_calls"] == 0


def test_budget_clipping_never_claims_unseen_source_as_covered():
    data, arm = fixture()
    result = read_contexts(data, arm, PROFILE, mode="parent", context_budget=32)
    assert result["per_query"]["q"]["PassageEvidenceCoverage@8"] == 0
    assert result["locations"]["q"][0]["matches"] == []
    assert result["per_query_cost"]["q"]["frame_estimated_tokens"] == 0
    assert read_frame(data["corpus"]["d"], 1, "s1", 0, 4, 32) is None


def test_overlapping_source_requests_merge_without_reordering_sections():
    matches = [{"section_id": "s2", "offset": 9, "end": 12,
                "read_window": {"offset": 4, "end": 20}},
               {"section_id": "s1", "offset": 0, "end": 4},
               {"section_id": "s2", "offset": 15, "end": 24,
                "read_window": {"offset": 10, "end": 30}}]
    assert merge_read_windows(matches) == [
        {"section_id": "s2", "offset": 4, "end": 30, "priority": 0},
        {"section_id": "s1", "offset": 0, "end": 4, "priority": 1}]
