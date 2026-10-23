"""Explicit RAG operations: inspect, probe, plan/execute incremental indexing, and query."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import httpx

from config import settings
from config.settings import Settings
from rag import vector_index as vectors
from rag.corpus import CorpusUnavailable
from rag.embedding_inputs import (
    InputProfileError,
    active_profile,
    document_text,
    query_text,
    vector_chunks,
)
from rag.search import RagIndex, last_route


def status() -> dict:
    store = vectors.get_store()
    store.load()
    view = store.view()
    policy = active_profile()
    return {
        "model": settings.embedding_model,
        "requested_dimensions": settings.embedding_dim,
        "actual_dimensions": view.dim if view else None,
        "profile_signature": policy.signature if policy else None,
        "index_directory": str(Path(settings.rag_vector_dir).resolve()),
        "route": vectors.route_status(),
    }


def probe(expected_dim: int, llama_tokenize: bool = False) -> dict:
    if not settings.embedding_configured:
        raise ValueError("embedding credentials/model are not configured")
    policy = active_profile()
    texts = [
        query_text("如何更新博客索引？", policy),
        document_text("博客索引", "正文变化后只重建变化片段的向量。", policy),
    ]
    values = vectors._embed_texts(texts, vectors.space_from_settings())
    valid = len(values) == 2 and all(
        row is not None
        and len(row) == expected_dim
        and any(row)
        and all(math.isfinite(x) for x in row)
        for row in values
    )
    result = {
        "ok": valid,
        "model": settings.embedding_model,
        "expected_dimensions": expected_dim,
        "returned_dimensions": [len(v) if v is not None else None for v in values],
        "input_tokens": [policy.count(t) for t in texts] if policy else None,
    }
    if llama_tokenize:
        if policy is None:
            raise ValueError("local tokenizer verification requires a pinned profile")
        samples = texts + [
            "测试[CLS] [SEP] <|endoftext|> Mixed 中文 é",
            "大小写 ABC xyz Café Straße naïve 👋 全角ＡＢＣ\n制表\t多个   空格",
            "中" * (policy.spec.max_tokens - 2),
        ]
        endpoint = settings.embedding_base_url.rstrip("/").removesuffix("/v1") + "/tokenize"
        equal = []
        with httpx.Client(
            timeout=settings.embedding_timeout,
            headers={"Authorization": "Bearer " + settings.embedding_api_key},
        ) as client:
            for text in samples:
                prepared = policy.normalize(text)
                response = client.post(
                    endpoint,
                    json={"content": prepared, "add_special": True, "parse_special": True},
                )
                response.raise_for_status()
                expected = policy.tokenizer.encode(
                    prepared, add_special_tokens=policy.spec.add_special_tokens
                ).ids
                equal.append(response.json().get("tokens") == expected)
        result["tokenizer_parity"] = equal
        result["ok"] = valid and all(equal)
    return result


def prepare(index: RagIndex, execute: bool, max_new: int) -> dict:
    if not vectors.is_enabled():
        raise ValueError("enable RAG_HYBRID_ENABLED and configure an embedding model first")
    chunks = vector_chunks(index._chunks)
    policy = active_profile()
    space = vectors.space_from_settings()
    texts = [document_text(c.get("title", ""), c["text"], policy) for c in chunks]
    store = vectors.get_store()
    store.load()
    _, _, rows = store._read_cache()
    retry = set(store.missing())
    keys = {vectors.chunk_key(space, t) for t in texts}
    pending = {k for k in keys if k not in rows or k in retry}
    report = {
        "documents": len(index._docs),
        "vector_chunks": len(chunks),
        "source_revision": index._revision,
        "unique_inputs": len(keys),
        "new_or_retry_inputs": len(pending),
        "execute": execute,
        "profile_signature": space.profile or None,
    }
    if not execute:
        return {**report, "ok": True}
    if len(pending) > max_new:
        raise ValueError("new-vector budget exceeded; inspect the plan before raising the limit")
    update = store.update(chunks, space)
    ok = bool(update.get("ok") or update.get("skipped") == "aligned") and not store.missing()
    return {**report, "ok": ok, "update": update, "status": status()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, help="same private EnvironmentFile used by Agent")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("status")
    inspect.add_argument("--require-vectors", action="store_true")
    check = commands.add_parser("probe")
    check.add_argument("--expected-dim", required=True, type=int)
    check.add_argument("--llama-tokenize", action="store_true", help="check local server token IDs")
    build = commands.add_parser("prepare")
    build.add_argument("--execute", action="store_true", help="default is a no-embedding plan")
    build.add_argument("--max-new", default=10000, type=int)
    query = commands.add_parser("query")
    query.add_argument("text")
    query.add_argument("--top-k", default=5, type=int)
    query.add_argument("--require-hybrid", action="store_true")
    args = parser.parse_args(argv)
    if (
        (args.command == "probe" and args.expected_dim < 1)
        or (args.command == "prepare" and args.max_new < 0)
        or (args.command == "query" and args.top_k < 1)
    ):
        parser.error("positive dimension/top-k and nonnegative max-new required")
    try:
        if args.env_file:
            if not args.env_file.is_file():
                raise ValueError("private EnvironmentFile does not exist")
            # Pydantic's generated field-only signature omits BaseSettings control arguments.
            options: dict = {"_env_file": (".env", args.env_file)}
            loaded = Settings(**options)
            for name in Settings.model_fields:
                setattr(settings, name, getattr(loaded, name))
            # tools.base captures BLOG_API_BASE at import; index construction happens later.
        if args.command == "status":
            result = status()
            if args.require_vectors:
                result["ok"] = result["route"]["active"] and result["route"]["missing"] == 0
        elif args.command == "probe":
            result = probe(args.expected_dim, args.llama_tokenize)
        else:
            # CLI query/plan must never start an implicit background corpus embedding job.
            index = RagIndex(vector_warmer=lambda _: None)
            index.build()
            if index.status()["stale"]:
                raise CorpusUnavailable("operator command requires a current complete corpus")
            if args.command == "prepare":
                result = prepare(index, args.execute, args.max_new)
            else:
                hits = index.search(args.text, args.top_k)
                route = last_route()
                result = {
                    "ok": hits is not None
                    and (not args.require_hybrid or route["mode"] == "hybrid"),
                    "route": route,
                    "hits": hits,
                    "index": index.status(),
                }
    except (CorpusUnavailable, InputProfileError, ValueError, OSError, httpx.HTTPError) as error:
        # Endpoint exceptions can contain URLs/credentials; keep stdout a safe diagnostic record.
        result = {"ok": False, "error_class": type(error).__name__}
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
