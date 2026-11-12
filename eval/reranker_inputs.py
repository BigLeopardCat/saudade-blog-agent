"""Freeze same-pool cross-encoder pairs from existing version-bound source locators."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path

os.environ["SAUDADE_IGNORE_ENV_FILE"] = "1"

from eval.chunk_context_comparison import ranked_ids, read_rows, source_hits  # noqa: E402
from eval.full_corpus_analysis import fusion  # noqa: E402
from eval.rag_dataset import load  # noqa: E402
from eval.replay_retrieval_ranks import read_frozen  # noqa: E402
from rag.sections import outline  # noqa: E402
from rag.vector_index import rrf_fuse  # noqa: E402

INPUT_POLICY = "first-production-rrf-locator-pair-v1"
PAIR_LIMIT = 512


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class PairEncoder:
    def __init__(self, tokenizer_path):
        from tokenizers import Tokenizer

        self.full = Tokenizer.from_file(str(tokenizer_path))
        self.full.no_truncation()
        self.full.no_padding()
        self.bounded = Tokenizer.from_file(str(tokenizer_path))
        self.bounded.no_padding()
        self.bounded.enable_truncation(max_length=PAIR_LIMIT, strategy="only_second")

    def encode(self, query, title, body, match):
        prefix = title[:400] + "\n"
        passage = prefix + body
        raw = self.full.encode(query, passage)
        query_ids = [v for v, sid in zip(raw.ids, raw.sequence_ids) if sid == 0]
        if len(query_ids) + self.full.num_special_tokens_to_add(is_pair=True) >= PAIR_LIMIT:
            raise ValueError("query alone exhausts the pair budget")
        encoded = self.bounded.encode(query, passage)
        kept_query = [v for v, sid in zip(encoded.ids, encoded.sequence_ids) if sid == 0]
        if not query_ids or query_ids != kept_query or len(encoded.ids) > PAIR_LIMIT:
            raise ValueError("full nonempty query and bounded pair required")
        offsets = [(max(0, lo - len(prefix)), min(len(body), hi - len(prefix)))
                   for sid, (lo, hi) in zip(encoded.sequence_ids, encoded.offsets)
                   if sid == 1 and hi > len(prefix) and hi > lo]
        visible = []
        if offsets:
            visible = [{"section_id": match["section_id"],
                        "offset": match["offset"] + min(a for a, _ in offsets),
                        "end": match["offset"] + max(b for _, b in offsets)}]
        return {"input_ids": encoded.ids, "input_tokens": len(encoded.ids),
                "full_pair_tokens": len(raw.ids), "query_tokens": len(query_ids),
                "truncated": len(raw.ids) > len(encoded.ids),
                "anchor": match, "visible_source": visible,
                "anchor_chars": len(body),
                "visible_source_chars": sum(m["end"] - m["offset"] for m in visible)}


def candidate_hits(data, lexical, vector, expected):
    hits = rrf_fuse(source_hits(data, lexical), source_hits(data, vector), 60, 100)
    if [row["id"] for row in hits] != expected or len(expected) != len(set(expected)):
        raise ValueError("candidate identities/order differ from fixed production RRF")
    if any(not row.get("matches") for row in hits):
        raise ValueError("candidate has no source locator")
    return hits


def prepare(data, summary, lexical, dense, encoder, output):
    frozen, _, ranked = read_frozen(summary)
    if frozen["input_hashes"] != data["hashes"]:
        raise ValueError("frozen retrieval dataset changed")

    @lru_cache(maxsize=2048)
    def sections(did):
        row = data["corpus"][did]
        return {s["section_id"]: s["text"] for s in outline(row["text"], row.get("title", ""))}

    count, truncated, tokens = 0, 0, 0
    with gzip.open(output, "wt", encoding="utf-8") as stream:
        for qid in sorted(data["queries"]):
            left = ranked_ids(ranked["bm25"][qid])
            right = dense[qid]["reference"]
            if (right != ranked_ids(ranked["vector_reference"][qid])
                    or [r["document_id"] for r in lexical[qid]["reference_locations"]]
                    != left[:24]):
                raise ValueError("baseline ranks changed")
            expected = fusion(left, right, 24)
            hits = candidate_hits(data, lexical[qid]["reference_locations"],
                                  dense[qid]["reference_locations"], expected)
            candidates = []
            for hit in hits:
                did, match = hit["id"], hit["matches"][0]
                text = sections(did)[match["section_id"]]
                start, end = match["offset"], match["end"]
                if not 0 <= start < end <= len(text):
                    raise ValueError("candidate source range is invalid")
                pair = encoder.encode(data["queries"][qid]["text"],
                                      data["corpus"][did].get("title", ""), text[start:end], match)
                candidates.append({"document_id": did, **pair,
                                   "retrieval_matches": hit["matches"]})
                count += 1
                truncated += pair["truncated"]
                tokens += pair["input_tokens"]
            stream.write(json.dumps({"query_id": qid, "candidates": candidates}) + "\n")
    return {"queries": len(data["queries"]), "candidate_pairs": count,
            "truncated_pairs": truncated, "unpadded_pair_tokens": tokens,
            "fixed_group_query_ids": frozen["group_query_ids"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--lexical", type=Path, required=True)
    parser.add_argument("--dense", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("do not overwrite frozen inputs")
    data = load(args.dataset, "dev", max_docs=100001)
    lexical, lex_meta = read_rows(args.lexical, data)
    dense, dense_meta = read_rows(args.dense, data)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = prepare(data, args.baseline, lexical, dense, PairEncoder(args.tokenizer), args.output)
    result.update(input_hashes=data["hashes"], input_policy=INPUT_POLICY,
                  tokenizer_sha256=sha256(args.tokenizer), pair_limit=PAIR_LIMIT,
                  output_sha256=sha256(args.output), baseline_sha256=sha256(args.baseline),
                  source_stages={"lexical": lex_meta, "dense": dense_meta},
                  model_calls=0, semantic_judge_calls=0)
    args.output.with_suffix(".metadata.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if not isinstance(v, dict)}), flush=True)


if __name__ == "__main__":
    main()
