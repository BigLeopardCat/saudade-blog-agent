"""Production feedback preserves provenance, missing context and conversation boundaries."""

import json

import pytest

from eval import golden_draft, online_baseline


def trace(tmp_path, index, cid=100, role="user", issue=False):
    doc = {"trace_id": f"fixture-{index}", "input": {
        "message": f"请求{index}", "principal_role": role, "principal_source": "assertion",
        "conversation_id": cid, "message_truncated": False}, "reply": "现场回复",
        "events": [{"node": "native", "event": "call", "name": "fixture",
                    "kind": "invalid_args" if issue else "ok"}]}
    path = tmp_path / f"20261027T00000{index}_7_fixture.json"
    path.write_text(json.dumps(doc))
    return path, doc


def test_missing_page_is_not_invented_as_home_and_requested_tools_are_not_observations(tmp_path):
    path, doc = trace(tmp_path, 1)
    doc["events"] = [{"node": "native", "event": "tool_requested", "name": "not_executed"}]
    record = golden_draft.build_record(path, doc, "20261027T000001", 7)
    assert record["context"] == {} and "page_context" in record["missing_evidence"]
    assert not record["tools"]


def test_current_page_and_request_truncation_are_preserved(tmp_path):
    path, doc = trace(tmp_path, 1)
    page = {"current_url": "/article/54", "page_title": "测试", "current_effects": "none",
            "current_darkmode": "off"}
    doc["input"].update(page_context=page, message_truncated=True)
    record = golden_draft.build_record(path, doc, "20261027T000001", 7)
    assert record["context"] == page
    assert record["missing_evidence"] == ["complete_user_request"]


def test_multiturn_context_cannot_cross_role_or_conversation(tmp_path):
    paths = [trace(tmp_path, index, cid=cid, role=role, issue=issue)[0]
             for index, cid, role, issue in [(1, 100, "user", False), (2, 100, "admin", True),
                                            (3, 101, "admin", True), (4, 100, "admin", True)]]
    flagged, ordinary, count = golden_draft.collect(paths, "20261027", "20261028")
    assert count == 4 and len(ordinary) == 1 and len(flagged) == 3
    assert flagged[0]["prior_turns"] == flagged[1]["prior_turns"] == []
    assert [row["trace_id"] for row in flagged[2]["prior_turns"]] == ["fixture-2"]


def test_known_evaluation_requests_can_be_excluded(tmp_path):
    path, _ = trace(tmp_path, 1, issue=True)
    assert golden_draft.collect([path], "20261027", "20261028", {"fixture-1"}) == ([], [], 0)


def test_draft_cannot_be_run_as_an_approved_baseline(tmp_path):
    path, doc = trace(tmp_path, 1, issue=True)
    record = golden_draft.build_record(path, doc, "20261027T000001", 7)
    generated = golden_draft.draft(record, 0, {})
    assert generated["gold"] == {} and generated["_draft"]["review_required"]
    suite = tmp_path / "suite.jsonl"
    suite.write_text(json.dumps(generated))
    with pytest.raises(ValueError, match="unreviewed draft"):
        online_baseline.load_suite(suite)


def test_existing_multiturn_requests_are_included_in_similarity_hints():
    inputs = golden_draft.load_golden_inputs()
    assert any(key.startswith("memory_recall:") for key in inputs)
    assert golden_draft.GOLDEN.name == "native_requests.jsonl"


def test_similarity_hints_share_the_canonical_inherited_first_turn_input(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps({"id": "follow-up", "user_input": "first query",
        "rounds": [{"round": 1}, {"round": 2, "user_input": "another keyword"}]}) + "\n")
    assert golden_draft.load_golden_inputs(path) == {
        "follow-up:1": "first query", "follow-up:2": "another keyword"}


@pytest.mark.parametrize("event", [
    {"node": "native", "event": "call", "kind": "unavailable"},
    {"node": "native", "event": "tool_error", "reason": "unknown_tool"},
    {"node": "native", "event": "call", "kind": "invalid_args"},
    {"node": "native", "event": "call", "kind": "error"},
    {"node": "native", "event": "budget_stop"},
    {"node": "native", "event": "llm_error"},
    {"node": "execute", "event": "tool_failed"},
])
def test_structured_failures_enter_feedback_candidates_without_reply_text(tmp_path, event):
    path, doc = trace(tmp_path, 1)
    doc["events"] = [event]
    path.write_text(json.dumps(doc))
    flagged, ordinary, scanned = golden_draft.collect([path], "20261027", "20261028")
    assert scanned == 1 and ordinary == [] and len(flagged) == 1
    assert flagged[0]["signals"] == ["runtime_or_tool_issue"]
    generated = golden_draft.draft(flagged[0], 0, {})
    assert generated["gold"] == {} and generated["_draft"]["review_required"]


@pytest.mark.parametrize("kind", ["ok", "empty", "prepared", "not_found", "denied"])
def test_expected_nonfailure_results_remain_ordinary_samples(tmp_path, kind):
    path, doc = trace(tmp_path, 1)
    doc["events"] = [{"node": "native", "event": "call", "kind": kind}]
    path.write_text(json.dumps(doc))
    flagged, ordinary, scanned = golden_draft.collect([path], "20261027", "20261028")
    assert scanned == 1 and flagged == [] and len(ordinary) == 1
    assert ordinary[0]["signals"] == []
