"""Compare fixed chunk policies and budgeted source reads without generated-answer scoring."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path

os.environ["SAUDADE_IGNORE_ENV_FILE"] = "1"

from eval.context_retrieval_eval import read_contexts  # noqa: E402
from eval.full_corpus_analysis import fusion  # noqa: E402
from eval.offline_rag_eval import evaluate  # noqa: E402
from eval.rag_dataset import load  # noqa: E402
from eval.replay_retrieval_ranks import read_frozen  # noqa: E402
from eval.statistical_metrics import paired_delta  # noqa: E402
from rag.corpus import content_revision  # noqa: E402
from rag.embedding_inputs import active_profile  # noqa: E402
from rag.sections import PARSER_VERSION  # noqa: E402
from rag.vector_index import rrf_fuse  # noqa: E402


def read_json(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def read_rows(path, data):
    metadata = read_json(path.with_suffix(".metadata.json"))
    if (metadata["input_hashes"] != data["hashes"]
            or metadata["output_sha256"] != hashlib.sha256(path.read_bytes()).hexdigest()):
        raise ValueError("locator source/output binding changed")
    with path.open() as stream:
        rows = [json.loads(line) for line in stream]
    indexed = {row["query_id"]: row for row in rows}
    if len(indexed) != len(rows) or indexed.keys() != data["queries"].keys():
        raise ValueError("locator queries duplicate, missing or foreign")
    return indexed, metadata


def ordinal(ids):
    return {did: -float(i + 1) for i, did in enumerate(ids)}


def ranked_ids(run):
    return sorted(run, key=lambda did: (-run[did], did))


def source_hits(data, rows):
    result = []
    for row in rows:
        did = row["document_id"]
        source = data["corpus"][did]
        revision = content_revision(source.get("title", ""), source["text"])
        ids = list(dict.fromkeys(m["section_id"] for m in row["matches"]))
        result.append({"type": "note", "id": did, "title": "",
            "corpus_revision": data["hashes"]["corpus.jsonl"],
            "content_revision": revision, "document_revision": revision,
            "section_schema": PARSER_VERSION, "sections": ids, "section_ids": ids,
            "matches": row["matches"]})
    return result


def hybrid_locations(data, lexical, dense, expected):
    hits = rrf_fuse(source_hits(data, lexical), source_hits(data, dense), 60, 8)
    if [row["id"] for row in hits] != expected[:8]:
        raise ValueError("location RRF differs from the fixed document fusion")
    return [{"document_id": row["id"], "matches": row["matches"]} for row in hits]


def du_views(data, summary, old_vector, new_vector, lexical):
    frozen, _, ranked = read_frozen(summary)
    if frozen["input_hashes"] != data["hashes"]:
        raise ValueError("full baseline dataset changed")
    views = {name: {"run": {}, "locations": {}} for name in
             ("bm25", "old_vector", "new_vector", "old_hybrid24", "new_hybrid24")}
    for qid in data["queries"]:
        left = ranked_ids(ranked["bm25"][qid])
        old, new = old_vector[qid]["reference"], new_vector[qid]["reference"]
        if old != ranked_ids(ranked["vector_reference"][qid]):
            raise ValueError("baseline exact-vector ranks changed while recovering locations")
        if [r["document_id"] for r in lexical[qid]["reference_locations"]] != left[:24]:
            raise ValueError("lexical source locations changed")
        for name, ids, locators in [
            ("bm25", left, lexical[qid]["reference_locations"]),
            ("old_vector", old, old_vector[qid]["reference_locations"]),
            ("new_vector", new, new_vector[qid]["reference_locations"]),
        ]:
            if [r["document_id"] for r in locators] != ids[:24]:
                raise ValueError("source locations do not match fixed candidates")
            views[name]["run"][qid], views[name]["locations"][qid] = ordinal(ids), locators[:8]
        for prefix, ids, rows in [("old", old, old_vector), ("new", new, new_vector)]:
            combined = fusion(left, ids, 24)
            name = prefix + "_hybrid24"
            views[name]["run"][qid] = ordinal(combined)
            views[name]["locations"][qid] = hybrid_locations(data,
                lexical[qid]["reference_locations"], rows[qid]["reference_locations"], combined)
    return views, frozen


def cmrc_views(data, before, after):
    if before["input_hashes"] != data["hashes"] or after["input_hashes"] != data["hashes"]:
        raise ValueError("CMRC dataset changed")
    for key in ("run", "locations"):
        if before["arms"]["bm25"][key] != after["arms"]["bm25"][key]:
            raise ValueError("fixed CMRC lexical route changed")
    return {"bm25": before["arms"]["bm25"],
            **{prefix + "_" + name: report["arms"][arm]
               for prefix, report in [("old", before), ("new", after)]
               for name, arm in [("vector", "vector"), ("hybrid24", "hybrid")]}}


def compare(data, views, profile, *, groups=None):
    documents = {name: evaluate(data, view["run"], [1, 3, 8, 24, 100])
                 for name, view in views.items()}
    contexts = {}
    for name, view in views.items():
        for mode in ("window", "parent", "neighbors"):
            contexts[name + "/" + mode] = read_contexts(data, view, profile, mode=mode)
            print(f"context replay {name}/{mode} ({len(data['queries'])} queries)", flush=True)
    order = list(data["queries"])
    paired = {}
    for family in ("vector", "hybrid24"):
        for metric in ("R@8", "nDCG@8", "Success@8"):
            paired[family + "/" + metric] = paired_delta(
                [documents["old_" + family]["per_query"][q][metric] for q in order],
                [documents["new_" + family]["per_query"][q][metric] for q in order])
        for mode in ("window", "parent", "neighbors"):
            old = contexts["old_" + family + "/" + mode]["per_query"]
            new = contexts["new_" + family + "/" + mode]["per_query"]
            metric = "PassageEvidenceCoverage@8"
            if all(metric in old[q] and metric in new[q] for q in order):
                paired[family + "/" + mode + "/" + metric] = paired_delta(
                    [old[q][metric] for q in order], [new[q][metric] for q in order])
    cohorts = {name: {group: {metric: sum(arm["per_query"][q][metric] for q in qids) / len(qids)
        for metric in ("R@8", "nDCG@8", "Success@8")}
        for group, qids in (groups or {}).items()} for name, arm in documents.items()}
    return {"dataset": data["manifest"], "input_hashes": data["hashes"],
            "documents": documents, "contexts": contexts, "paired_comparisons": paired,
            "fixed_group_query_ids": groups or {}, "groups": cohorts,
            "queries": len(order), "semantic_judge_calls": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--du", type=Path, required=True)
    parser.add_argument("--cmrc", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--old-vector", type=Path, required=True)
    parser.add_argument("--new-vector", type=Path, required=True)
    parser.add_argument("--lexical", type=Path, required=True)
    parser.add_argument("--old-cmrc", type=Path, required=True)
    parser.add_argument("--new-cmrc", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    du = load(args.du, "dev", max_docs=100001)
    old, old_meta = read_rows(args.old_vector, du)
    new, new_meta = read_rows(args.new_vector, du)
    lexical, lex_meta = read_rows(args.lexical, du)
    if new_meta.get("postgres_verification_queries", 0) < 32:
        raise ValueError("explicit new-index production SQL controls are incomplete")
    views, frozen = du_views(du, args.baseline, old, new, lexical)
    profile = active_profile()
    if profile is None:
        raise ValueError("pinned tokenizer profile required for context costing")
    result = {"schema_version": 1,
        "config": {"fusion": "equal-weight document/section/location RRF", "rrf_k": 60,
                   "candidates_per_route": 24, "output_documents": 8,
                   "query_frame_estimated_token_budget": 4096, "expansion_bge_tokens": 1024},
        "du": compare(du, views, profile, groups=frozen["group_query_ids"]),
        "stages": {"old_vector": old_meta, "new_vector": new_meta, "lexical": lex_meta}}
    del du, views, old, new, lexical, frozen
    cmrc = load(args.cmrc, "dev")
    before, after = read_json(args.old_cmrc), read_json(args.new_cmrc)
    result["cmrc"] = compare(cmrc, cmrc_views(cmrc, before, after), profile)
    result["semantic_judge_calls"] = 0
    result["scope"] = ("Full source-dev doc retrieval plus known-source span diagnosis. "
        "Fixed fusion; deterministic source-read replay, no generated answers, HTTP or Judge. "
        "Context estimates exclude prompt/history/schema and are not provider bills.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8", compresslevel=6) as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print("fixed-policy comparison complete", flush=True)


if __name__ == "__main__":
    main()
