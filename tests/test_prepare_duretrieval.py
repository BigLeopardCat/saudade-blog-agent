"""Public-data preparation verifies pinned inputs before publishing a fixed subset."""

import hashlib
import json

import pytest

from eval import prepare_duretrieval as prepare


def test_preparation_preserves_selected_judgments_and_rejects_changed_source(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    raw = source / "pinned.parquet"
    raw.write_bytes(b"synthetic source identity")
    monkeypatch.setattr(prepare, "FILES", {
        raw.name: hashlib.sha256(raw.read_bytes()).hexdigest()})
    selected = {"corpus": {"a": {"_id": "a", "text": "fact"},
                            "b": {"_id": "b", "text": "background"}},
                "queries": {"q": {"_id": "q", "text": "question"}},
                "qrels": {"q": {"a": 2, "b": 0}}, "hard": {"q": None},
                "background": ["b"]}
    seen = []
    monkeypatch.setattr(prepare, "select", lambda *args: seen.append(args) or selected)
    output = tmp_path / "prepared"
    result = prepare.prepare(source, output, seed="fixed", queries=1, background=1, pool=2)
    assert result["documents"] == 2 and result["queries"] == 1
    assert seen[0][1:] == ("fixed", 1, 1, 2)
    assert (output / "qrels/dev.tsv").read_text().endswith("q\ta\t2\nq\tb\t0\n")
    assert "not an official" in json.loads((output / "manifest.json").read_text())["scope"]
    raw.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed pinned"):
        prepare.prepare(source, tmp_path / "bad", seed="fixed", queries=1, background=1, pool=2)
    assert not (tmp_path / "bad").exists()
