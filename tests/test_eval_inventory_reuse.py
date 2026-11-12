"""Offline inventory compression and cross-policy reuse cannot change the encoder."""

import gzip
import json
from types import SimpleNamespace

import pytest

from eval.full_corpus_cache import inventory_path, inventory_rows, same_encoder
from rag.embedding_inputs import ProfileSpec


def test_gzip_and_plain_inventories_preserve_identical_input_rows(tmp_path):
    row = {"key": "synthetic", "input": "段落\r\n原文", "chunk": {"start": 2, "end": 8}}
    path = tmp_path / "inventory.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    assert inventory_path(tmp_path) == path and list(inventory_rows(path)) == [row]
    (tmp_path / "inventory.jsonl").write_text(json.dumps(row) + "\n")
    with pytest.raises(ValueError, match="one explicit"):
        inventory_path(tmp_path)


def test_encoder_reuse_accepts_only_splitting_and_artifact_path_changes():
    old = ProfileSpec(schema_version=1, model_revision="fixed-weight-sha", tokenizer_file="a",
                      tokenizer_sha256="0" * 64, max_tokens=512, pooling="cls", normalization="L2")
    changed = old.model_copy(update={"tokenizer_file": "b",
                                     "chunking_strategy": "sentence-paragraph-v1"})
    same_encoder(SimpleNamespace(spec=old), SimpleNamespace(spec=changed))
    for change in [{"pooling": "mean"}, {"model_revision": "other"}, {"query_prefix": "other"},
                   {"max_tokens": 1024}, {"unicode_normalization": "NFC"}]:
        with pytest.raises(ValueError, match="encoder changed"):
            same_encoder(SimpleNamespace(spec=old),
                         SimpleNamespace(spec=changed.model_copy(update=change)))
