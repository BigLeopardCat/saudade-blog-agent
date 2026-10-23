"""Explicit live-service regression for source coverage, budgets and incremental reuse."""

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["SAUDADE_IGNORE_ENV_FILE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings  # noqa: E402
from rag.corpus import content_revision  # noqa: E402
from rag.embedding_inputs import active_profile, document_text, vector_chunks  # noqa: E402
from rag.search import RagIndex  # noqa: E402
from rag.vector_index import VectorStore, embed_query, space_from_settings  # noqa: E402


def verify(root: Path) -> dict:
    policy = active_profile()
    if policy is None:
        raise ValueError("a pinned input profile is required")
    fixture = Path(__file__).parent / "fixtures/embedding_probe.jsonl"
    body = next(
        json.loads(s)["text"]
        for s in fixture.read_text().splitlines()
        if json.loads(s)["_id"] == "document-zh-1800"
    )
    body += "TAIL"
    title, content = "预算验证", "## 正文\n" + body
    documents = [
        {
            "type": "note",
            "id": 1,
            "title": title,
            "content": content,
            "revision": content_revision(title, content),
        }
    ]
    index = RagIndex(corpus_loader=lambda: documents, vector_warmer=lambda _: None)
    index.build()
    chunks = vector_chunks(index._chunks)
    covered = set()
    for chunk in chunks:
        if chunk["text"][chunk["prefix_chars"] :] != body[chunk["start"] : chunk["end"]]:
            raise ValueError("source range mismatch")
        covered.update(range(chunk["start"], chunk["end"]))
        policy.validate(document_text(title, chunk["text"], policy))
    if covered != set(range(len(body))) or not any("TAIL" in c["text"] for c in chunks):
        raise ValueError("source coverage/tail missing")
    store = VectorStore(root)
    first = store.update(chunks, space_from_settings())
    if not first.get("ok") or first.get("missing"):
        raise ValueError("live embedding did not complete")
    second = store.update(chunks, space_from_settings())
    if second.get("skipped") != "aligned" or second["api_calls"]:
        raise ValueError("unchanged inputs were re-embedded")
    query = embed_query("如何增量更新博客向量索引？")
    if query is None or store.view() is None or len(query) != store.view().dim:
        raise ValueError("query/document dimensions do not align")
    return {
        "scope": "functional regression only; no retrieval-quality/performance score",
        "model": settings.embedding_model,
        "profile": policy.spec.model_dump(),
        "profile_signature": policy.signature,
        "source_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "source_characters": len(body),
        "source_tokens_without_metadata": policy.count(body),
        "lexical_chunks": len(index._chunks),
        "vector_chunks": len(chunks),
        "embedding_input_tokens": [
            policy.count(document_text(title, c["text"], policy)) for c in chunks
        ],
        "complete_source_coverage": True,
        "tail_preserved": True,
        "first_update": first,
        "second_update": second,
        "query_dimension": len(query),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    settings.embedding_profile = args.profile
    settings.embedding_base_url = args.base_url
    settings.embedding_api_key = os.getenv("EMBEDDING_API_KEY") or "local-only"
    settings.embedding_model = args.model
    settings.embedding_batch_size = 1
    settings.embedding_timeout = args.timeout
    with tempfile.TemporaryDirectory(prefix="rag-budget-") as temporary:
        settings.rag_vector_dir = temporary
        report = verify(Path(temporary))
    root = Path(__file__).resolve().parents[1]
    sources = [
        root / "rag" / name
        for name in ("embedding_inputs.py", "vector_index.py", "chunks.py", "search.py")
    ]
    report["code_sha256"] = hashlib.sha256(b"".join(p.read_bytes() for p in sources)).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
