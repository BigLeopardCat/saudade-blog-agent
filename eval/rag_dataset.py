"""Strict local BEIR corpus/queries/qrels reader; never downloads evaluation data."""

import csv
import hashlib
import json
from pathlib import Path


def _rows(path: Path, limit: int) -> dict[str, dict]:
    rows = {}
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path.name}: expected an object")
            key = row.get("_id")
            if not isinstance(key, str) or not key or key in rows:
                raise ValueError(f"{path.name}: missing or duplicate string _id")
            if not isinstance(row.get("text"), str) or not row["text"].strip():
                raise ValueError(f"{path.name}: empty/non-string text")
            if not isinstance(row.get("title", ""), str):
                raise ValueError(f"{path.name}: non-string title")
            rows[key] = row
            if len(rows) > limit:
                raise ValueError(f"{path.name}: exceeds limit {limit}; prepare an explicit subset")
    if not rows:
        raise ValueError(f"{path.name}: empty input")
    return rows


def load(root: Path, split: str, max_docs: int = 10000) -> dict:
    if not split or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in split):
        raise ValueError("invalid split name")
    paths = [
        root / "manifest.json",
        root / "corpus.jsonl",
        root / "queries.jsonl",
        root / "qrels" / f"{split}.tsv",
    ]
    manifest = json.loads(paths[0].read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or any(
        not isinstance(manifest.get(k), str) or not manifest[k].strip()
        for k in ("name", "source", "revision", "license", "scope")
    ):
        raise ValueError("manifest requires schema 1, name/source/revision/license/scope")
    corpus = _rows(paths[1], max_docs)
    queries = _rows(paths[2], 10000)
    qrels: dict[str, dict[str, int]] = {}
    with paths[3].open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if reader.fieldnames != ["query-id", "corpus-id", "score"]:
            raise ValueError("expected BEIR TSV header: query-id, corpus-id, score")
        for row in reader:
            qid, did, grade = row["query-id"], row["corpus-id"], int(row["score"])
            if qid not in queries or did not in corpus or grade < 0:
                raise ValueError("qrels references unknown query/document or negative grade")
            judged = qrels.setdefault(qid, {})
            if did in judged:
                raise ValueError("duplicate qrel")
            judged[did] = grade
    if not qrels:
        raise ValueError("no judged queries")
    selected = {qid: queries[qid] for qid in qrels}
    for qid, query in selected.items():
        _validate_evidence(query, qrels[qid])
    hashes = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths
    }
    return {
        "manifest": manifest,
        "hashes": hashes,
        "corpus": corpus,
        "queries": selected,
        "qrels": qrels,
    }


def _validate_evidence(query: dict, judgments: dict[str, int]) -> None:
    positive = {did for did, grade in judgments.items() if grade > 0}
    if not positive and query.get("answerable") is not False:
        raise ValueError("zero-positive query must explicitly declare answerable=false")
    if positive and query.get("answerable") is False:
        raise ValueError("unanswerable query has positive judgments")
    groups = query.get("evidence_groups", [])
    if not isinstance(groups, list) or any(
        not isinstance(g, list)
        or not g
        or any(not isinstance(x, str) for x in g)
        or not set(g) <= positive
        for g in groups
    ):
        raise ValueError("evidence_groups must contain nonempty lists of positive document IDs")
