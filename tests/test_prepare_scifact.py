"""Public-data preparation must preserve the full corpus and reject changed inputs."""

import hashlib
import json
import zipfile

import pytest

from eval import prepare_scifact as scifact
from eval.rag_dataset import load


def archive(tmp_path, monkeypatch, documents=5183):
    path = tmp_path / "scifact.zip"
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(
            "scifact/corpus.jsonl",
            "\n".join(json.dumps({"_id": str(i), "text": "test text"}) for i in range(documents)),
        )
        bundle.writestr(
            "scifact/queries.jsonl",
            "\n".join(json.dumps({"_id": str(i), "text": "test query"}) for i in range(301)),
        )
        bundle.writestr(
            "scifact/qrels/test.tsv",
            "query-id\tcorpus-id\tscore\n" + "".join(f"{i}\t{i}\t1\n" for i in range(300)),
        )
        bundle.writestr("scifact/../../unexpected.txt", "must not be extracted")
    monkeypatch.setattr(scifact, "SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    return path


def test_full_dataset_keeps_background_documents_and_query_split(tmp_path, monkeypatch):
    source = archive(tmp_path, monkeypatch)
    target = tmp_path / "prepared"
    scifact.prepare(source, target)
    data = load(target, "test")
    assert len(data["corpus"]) == 5183  # Includes 4883 documents with no positive test judgment.
    assert len(data["queries"]) == 300  # Does not evaluate the extra, unjudged query.
    assert data["qrels"]["299"] == {"299": 1}
    assert not (tmp_path / "unexpected.txt").exists()
    with pytest.raises(FileExistsError):
        scifact.prepare(source, target)


def test_modified_archive_and_incomplete_corpus_publish_nothing(tmp_path, monkeypatch):
    source = archive(tmp_path, monkeypatch, documents=300)
    target = tmp_path / "prepared"
    with pytest.raises(ValueError, match="cardinality"):
        scifact.prepare(source, target)
    assert not target.exists()
    source.write_bytes(source.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="SHA-256"):
        scifact.prepare(source, target)
    assert not target.exists()


def test_network_requires_explicit_download_choice(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("must not download without --download")

    monkeypatch.setattr(scifact, "download", forbidden)
    with pytest.raises(SystemExit):
        scifact.main(["--output", str(tmp_path / "prepared")])
