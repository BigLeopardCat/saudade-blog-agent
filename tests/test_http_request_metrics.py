"""JSON-channel totals need explicit request coverage and valid event measurements."""

import json

import pytest

from eval import request_metrics

FIELDS = ("http_attempts", "http_retry_attempts", "http_response_bytes",
          "http_incomplete_attempts")


def event(attempt=1, size=0, complete=True, **kwargs):
    return {"node": "tools", "event": "http_attempt", "attempt": attempt,
            "response_bytes": size, "response_complete": complete, **kwargs}


def document(events=(), marker=1):
    return {"observability": {"json_http": marker}, "events": list(events)}


def save(tmp_path, name, doc):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(doc))
    return str(path)


def test_tool_requests_and_json_attempts_retries_bytes_are_separate():
    row = request_metrics.summary(document([
        {"node": "native", "event": "tool_requested"},
        event(size=24, complete=False), event(attempt=2, size=100), event(size=80),
        event(size=999999, node="model"),
    ]))
    assert row["tool_requests"] == 1 and row["http_observed_requests"] == 1
    assert row["http_attempts"] == row["http_attempts_sum"] == 3
    assert row["http_retry_attempts"] == row["http_retry_attempts_sum"] == 1
    assert row["http_response_bytes"] == row["http_response_bytes_sum"] == 204
    assert row["http_incomplete_attempts"] == row["http_incomplete_attempts_sum"] == 1


def test_supported_observation_marker_distinguishes_known_json_zero_from_old_unknown():
    current = request_metrics.summary(document())
    archived = request_metrics.summary({"events": []})
    assert current["http_observed_requests"] == 1
    assert archived["http_observed_requests"] == 0
    for field in FIELDS:
        assert current[field] == current[field + "_sum"] == 0
        assert archived[field] is None and archived[field + "_sum"] == 0


@pytest.mark.parametrize("marker", [None, 0, 2, True, "1", {}, []])
def test_missing_or_unsupported_marker_cannot_claim_complete_request_coverage(marker):
    row = request_metrics.summary(document([event(attempt=2, size=7, complete=False)], marker))
    assert row["http_observed_requests"] == 0
    assert all(row[field] is None for field in FIELDS)
    assert [row[field + "_sum"] for field in FIELDS] == [1, 1, 7, 1]


@pytest.mark.parametrize("events", [None, {}, "events", [None], [event(size=9), "invalid"]])
def test_invalid_event_container_cannot_turn_missing_observations_into_zero(events):
    doc = document()
    doc["events"] = events
    row = request_metrics.summary(doc)
    assert row["http_observed_requests"] == 0
    assert all(row[field] is None for field in FIELDS)


@pytest.mark.parametrize(("key", "value", "metric"), [
    *(('attempt', value, 'http_retry_attempts') for value in (None, 0, -1, True, '2', 1.5)),
    *(('response_bytes', value, 'http_response_bytes')
      for value in (None, -1, True, '7', 1.5)),
    *(('response_complete', value, 'http_incomplete_attempts') for value in (None, 0, 1, 'false')),
])
def test_invalid_measurement_stays_unknown_and_does_not_erase_valid_subtotals(key, value, metric):
    invalid = {**event(), key: value}
    row = request_metrics.summary(document([event(attempt=2, size=11, complete=False), invalid]))
    assert row["http_attempts"] == 2 and row["http_observed_requests"] == 1
    assert row[metric] is None
    assert row[metric + "_sum"] == (11 if metric == "http_response_bytes" else 1)


def test_collect_mixed_coverage_preserves_subtotals_and_unknown_full_totals(tmp_path):
    paths = [save(tmp_path, "measured", document([event(size=12), event(2, 3, False)])),
             save(tmp_path, "known-zero", document()),
             save(tmp_path, "old", {"events": []}),
             save(tmp_path, "event-only", document([event(size=6)], marker=None))]
    group, = request_metrics.collect(paths)["groups"]
    assert group["requests"] == 4 and group["http_observed_requests"] == 2
    assert all(group[field] is None for field in FIELDS)
    assert [group[field + "_sum"] for field in FIELDS] == [3, 1, 21, 1]


def test_collect_covered_requests_reports_exact_totals_and_preserves_known_zero(tmp_path):
    paths = [save(tmp_path, "read", document([event(size=8), event(size=8)])),
             save(tmp_path, "retry", document([event(1, 0, False), event(2, 30)])),
             save(tmp_path, "chat", document())]
    group, = request_metrics.collect(paths)["groups"]
    assert group["requests"] == group["http_observed_requests"] == 3
    assert [group[field] for field in FIELDS] == [4, 1, 46, 1]
    assert [group[field + "_sum"] for field in FIELDS] == [4, 1, 46, 1]


def test_invalid_bytes_do_not_taint_independently_valid_attempt_and_retry_totals(tmp_path):
    paths = [save(tmp_path, "valid", document([event(size=10)])),
             save(tmp_path, "invalid", document([event(2, "unknown", False)]))]
    group, = request_metrics.collect(paths)["groups"]
    assert group["http_observed_requests"] == 2
    assert group["http_attempts"] == 2 and group["http_retry_attempts"] == 1
    assert group["http_incomplete_attempts"] == 1
    assert group["http_response_bytes"] is None and group["http_response_bytes_sum"] == 10
