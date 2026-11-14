"""Read diagnostics track delivered evidence, not tool names or answer correctness."""

import pytest
from langchain_core.messages import ToolMessage

from eval import request_metrics
from eval.retrieval_evidence import reading_usage
from rag.reading import read_observation
from rag.sections import PARSER_VERSION


def span(start=0, end=100, **changes):
    return {"type": "note", "id": 19, "revision": "a" * 64,
            "section_schema": PARSER_VERSION, "section_id": "s1",
            "offset": start, "end": end, **changes}


def observed(ident, operation, ranges, **changes):
    name = "rag_search" if operation == "retrieval" else "get_article_detail"
    return [{"event": "tool_requested", "name": name, "call_id": ident},
            {"event": "article_observation", "name": name, "call_id": ident,
             "kind": "ok", "operation": operation, "ranges": ranges, **changes}]


def test_partial_overlap_counts_only_repeated_visible_text():
    events = [*observed("search", "retrieval", [span()]), {"event": "llm_start"},
              *observed("read1", "body", [span(80, 150)]), {"event": "llm_start"},
              *observed("read2", "body", [span(90, 140)])]
    measured = request_metrics.summary({"events": events})["article_usage"]
    assert measured == {"requested": 3, "observed": 3, "coverage_complete": True,
                        "retrieved_chars": 100, "body_chars": 120, "repeated_chars": 70,
                        "body_calls_after_retrieval": 2, "model_calls_after_retrieval": 2}


@pytest.mark.parametrize("change", [{"id": 20}, {"revision": "b" * 64},
                                    {"section_id": "s2"}, {"section_id": ""}])
def test_other_documents_versions_and_coordinate_spaces_are_not_duplicate_reads(change):
    events = [*observed("search", "retrieval", [span()]),
              *observed("read", "body", [span(**change)])]
    assert reading_usage(events)["repeated_chars"] == 0


@pytest.mark.parametrize("changes", [None, {"ranges": None},
                                      {"ranges": [span(offset=True)]}, {"call_id": "wrong"}])
def test_missing_malformed_and_unmatched_observations_remain_unknown(changes):
    events = observed("read", "body", [span()])
    if changes is None:
        events.pop()
    else:
        events[-1].update(changes)
    measured = reading_usage(events)
    assert not measured["coverage_complete"]
    assert measured["body_chars"] is measured["repeated_chars"] is None


def test_empty_and_failed_retrieval_do_not_claim_body_or_followup_evidence():
    events = [*observed("search", "retrieval", [], kind="unavailable"), {"event": "llm_start"}]
    measured = reading_usage(events)
    assert measured["coverage_complete"] and measured["retrieved_chars"] == 0
    assert measured["model_calls_after_retrieval"] == 0


def test_telemetry_retains_visible_coordinates_without_body_or_query_text():
    data = {"view": "body", "source": span(end=5), "sectionText": "hello"}
    value = ToolMessage(content="truncated display", tool_call_id="read",
                        artifact={"kind": "ok", "data": data})
    event = read_observation({"name": "get_article_detail", "id": "read", "args": {}}, value)
    assert event["returned_chars"] == 5 and event["ranges"] == [span(end=5)]
    assert "hello" not in str(event) and "content" not in event and "query" not in event
    value.artifact["data"]["sectionText"] = "inconsistent"
    assert read_observation({"name": "get_article_detail", "id": "read", "args": {}}, value)[
        "ranges"] is None
