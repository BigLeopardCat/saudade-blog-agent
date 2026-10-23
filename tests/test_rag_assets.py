"""Pinned downloads and immutable runtime verification fail closed."""

import hashlib
import io
import json
import tarfile

import httpx
import pytest

from eval import prepare_t2_subset as t2
from scripts import prepare_embedding_assets as assets


def test_existing_asset_mismatch_requires_explicit_download(tmp_path, monkeypatch):
    path = tmp_path / "model.gguf"
    path.write_bytes(b"corrupt")
    model = {"filename": path.name, "sha256": hashlib.sha256(b"correct").hexdigest()}

    def forbidden(*args, **kwargs):
        pytest.fail("network access without explicit --download")

    monkeypatch.setattr(httpx, "stream", forbidden)
    with pytest.raises(ValueError, match="missing/changed"):
        assets.acquire(tmp_path, model, False)
    assert path.read_bytes() == b"corrupt"
    path.write_bytes(b"correct")
    assert assets.acquire(tmp_path, model, False) == path


def test_prepared_runtime_is_verified_without_overwriting_running_files(tmp_path):
    archive = tmp_path / "runtime.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        member = tarfile.TarInfo("llama-b11829/llama-server")
        member.size = 6
        member.mode = 0o755
        bundle.addfile(member, io.BytesIO(b"binary"))
    server = assets.runtime_ready(tmp_path, archive, False)
    before = server.stat()
    assert assets.runtime_ready(tmp_path, archive, True) == server
    assert server.stat().st_ino == before.st_ino
    server.write_bytes(b"tamper")
    with pytest.raises(ValueError, match="changed immutable runtime"):
        assets.runtime_ready(tmp_path, archive, False)
    assert server.read_bytes() == b"tamper"


@pytest.fixture
def t2_source(monkeypatch):
    sources = {
        "data": [
            {"query-id": "q1", "corpus-id": "a", "score": 2},
            {"query-id": "q1", "corpus-id": "b", "score": 1},
            {"query-id": "q2", "corpus-id": "a", "score": 1},
            {"query-id": "q2", "corpus-id": "c", "score": 0},
        ],
        "queries": [{"_id": q, "text": "更新博客"} for q in ["q1", "q2"]],
        "corpus": [
            {"_id": d, "title": "", "text": "中文" * (50 if d == "c" else 2)}
            for d in ["a", "b", "c", "d", "e", "f"]
        ],
    }
    monkeypatch.setattr(t2, "rows", lambda p: iter(sources[p.parent.name]))
    # No unjudged lexical overlap must not cause a selected query to disappear.
    monkeypatch.setattr(t2, "bm25_run", lambda data, **kw: ({q: {} for q in data["queries"]}, {}))
    return sources


def test_subset_preserves_all_judgments_and_queries_without_lexical_hits(t2_source, tmp_path):
    data = t2.select(tmp_path, "seed", 2, 2, 6)
    assert len(data["queries"]) == 2 and set(data["corpus"]) >= {"a", "b", "c"}
    assert data["qrels"]["q1"] == {"a": 2, "b": 1}
    assert data["qrels"]["q2"] == {"a": 1, "c": 0}
    assert data["hard"] == {"q1": None, "q2": None}
    assert data == t2.select(tmp_path, "seed", 2, 2, 6)


def test_short_stratum_never_discards_a_long_judged_document(t2_source, tmp_path):
    data = t2.select(tmp_path, "seed", 1, 2, 6, 8)
    assert list(data["queries"]) == ["q1"]
    assert data["qrels"]["q1"] == {"a": 2, "b": 1}
    assert all(len(row["text"]) <= 8 for row in data["corpus"].values())
    with pytest.raises(ValueError, match="not enough judged"):
        t2.select(tmp_path, "seed", 2, 2, 6, 8)


def test_prepared_subset_round_trips_standard_beir_loader(t2_source, tmp_path):
    output = tmp_path / "subset"
    result = t2.prepare(tmp_path, output, "seed", 2, 2, 6)
    assert result["queries"] == 2
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["revision"] == t2.REVISION
    with pytest.raises(FileExistsError):
        t2.prepare(tmp_path, output, "seed", 2, 2, 6)
