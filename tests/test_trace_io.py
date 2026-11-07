"""Trace reading preserves evidence and rejects unreadable or malformed documents."""

import gzip
import importlib
import json
from pathlib import Path

import pytest

from eval import golden_trace
from utils import trace as trace_mod
from utils import trace_files
from utils.trace_io import load_trace

ROOT = Path(__file__).resolve().parent.parent


def test_json_and_gzip_preserve_identical_evidence(tmp_path):
    document = {"input": {"message": "公开合成请求"},
                "events": [{"node": "native", "event": "llm_done", "input": 100}],
                "reply": "喵"}
    data = json.dumps(document, ensure_ascii=False).encode()
    plain = tmp_path / "20261107T010000_7_12345678.json"
    compressed = plain.with_suffix(".json.gz")
    plain.write_bytes(data)
    compressed.write_bytes(gzip.compress(data))

    assert load_trace(plain) == load_trace(compressed) == document
    assert set(trace_files.iter_trace_files(str(tmp_path))) == {str(plain), str(compressed)}
    assert trace_files.parse_trace_name(str(compressed)) == ("20261107T010000", "7")
    assert trace_files.parse_stamp(str(compressed)) == "20261107T010000"
    assert trace_files.parse_trace_name("invalid-name.txt") == ("", "")


@pytest.mark.parametrize(("name", "data"), [
    ("bad.json", b"not json"),
    ("bad.json.gz", b"not gzip"),
    ("truncated.json.gz", gzip.compress(b'{"events": []}')[:-4]),
    ("bad_utf8.json", b"\xff"),
    ("list.json", b"[]"),
    ("null.json", b"null"),
    ("events.json", b'{"events": {}}'),
    ("event.json", b'{"events": [1]}'),
])
def test_unreadable_or_malformed_data_is_unknown(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    assert load_trace(path) is None


def test_missing_file_is_unknown(tmp_path):
    assert load_trace(tmp_path / "missing.json") is None


def test_reader_does_not_invent_or_rewrite_archived_fields(tmp_path):
    document = {"events": [{"node": "planner", "event": "decision", "skill": "chat"},
                            {"skill": "old", "workflow": "new"}], "reply": "archive"}
    path = tmp_path / "archive.json"
    path.write_text(json.dumps(document))
    assert load_trace(path) == document
    assert path.read_text() == json.dumps(document)

    document = {"reply": "no version or events metadata"}
    path.write_text(json.dumps(document))
    assert load_trace(path) == document


def test_recorder_stamps_schema_and_exposes_only_live_event_copies(tmp_path):
    trace_id = "trace-reader-contract"
    assert isinstance(trace_mod.SCHEMA_VERSION, int) and trace_mod.SCHEMA_VERSION >= 1
    trace_mod.start_trace(trace_id, user_id=0, thread_id="fixture", dir=str(tmp_path),
                          name="trace-reader-contract", by_day=False)
    try:
        trace_mod.record("native", "tool_end", name="search_articles", status="success")
        events = golden_trace.events_of(trace_id)
        assert len(events) == 1 and events[0]["event"] == "tool_end"
        events.append({"node": "injected", "event": "should-not-persist"})
        assert len(golden_trace.events_of(trace_id)) == 1
    finally:
        path = trace_mod.finish_trace(trace_id, "producer_done", 1.0, 2)

    document = load_trace(path)
    assert document is not None and document.get("schema_version") == trace_mod.SCHEMA_VERSION
    assert golden_trace.events_of(trace_id) == []
    assert golden_trace.events_of("absent-trace") == []


@pytest.mark.parametrize("module", [
    "eval.trace_alert", "eval.trace_metrics", "eval.trace_reconcile", "eval.golden_draft",
])
def test_diagnostic_readers_share_evidence_loading(module):
    assert importlib.import_module(module).load_trace is load_trace


def test_live_event_inspection_is_not_used_by_production():
    callers = [str(path.relative_to(ROOT))
               for directory in ("agent", "tools", "utils")
               for path in (ROOT / directory).rglob("*.py")
               if path.name != "trace.py" and "events_of" in path.read_text()]
    assert callers == []
