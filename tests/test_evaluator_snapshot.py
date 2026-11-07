"""Frozen evaluator evidence includes the helpers that determine pass/fail."""

import hashlib
from pathlib import Path

import pytest

from eval import online_baseline


def test_snapshot_freezes_shared_stream_validation_and_event_contract(tmp_path):
    hashes = online_baseline.snapshot(tmp_path, [{"id": "snapshot-probe"}])
    for name in ("stream_protocol.py", "agent/contracts/events.py",
                 "command_report.py", "acceptance.py", "corpus_terms.py",
                 "corpus_provenance.py", "golden_runtime.py", "golden_thread.py",
                 "golden_trace.py", "native_receipt_contract.py", "cache_diagnostics.py"):
        path = tmp_path / "evaluator" / name
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == hashes[name]



@pytest.mark.parametrize("helper", ["command_report.py", "acceptance.py"])
def test_snapshot_hash_tracks_assertion_and_semantic_helpers(tmp_path, monkeypatch, helper):
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(online_baseline, "ROOT", source)
    real_root = Path(__file__).resolve().parents[1]
    (source / "eval").mkdir()
    for path in (real_root / "eval").glob("*.py"):
        (source / "eval" / path.name).write_bytes(path.read_bytes())
    for name in ("release_profile.json",):
        (source / "eval" / name).write_bytes((real_root / "eval" / name).read_bytes())
    event = source / "agent/contracts/events.py"
    event.parent.mkdir(parents=True)
    event.write_bytes((real_root / "agent/contracts/events.py").read_bytes())
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    first = online_baseline.snapshot(before, [])
    changed = source / "eval" / helper
    changed.write_bytes(changed.read_bytes() + b"\n# Snapshot provenance probe.\n")
    second = online_baseline.snapshot(after, [])
    assert first[helper] != second[helper]
    assert {key for key in first if first[key] != second[key]} == {helper}
