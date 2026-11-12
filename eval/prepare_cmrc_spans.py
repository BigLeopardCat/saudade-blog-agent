"""Adapt pinned CMRC dev answer spans to a known-source location diagnostic, not a leaderboard."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from eval.rag_dataset import load
from rag.corpus import content_revision
from rag.sections import PARSER_VERSION, outline

REVISION = "c0eb1b6ba219847457e6af3180da722bbeb656af"
SOURCE = "https://github.com/ymcui/cmrc2018"
SHA256 = "e9ff74231f05c230c6fa88b84441ee334d97234cbb610991cd94b82db00c7f1f"


def anchor(context, answer):
    quote, start = answer["text"], answer["answer_start"]
    if not quote.strip():
        return None, "empty_quote"
    if type(start) is int and start >= 0 and context[start:start + len(quote)] == quote:
        return start, "original_offset"
    start = context.find(quote)
    if start >= 0 and context.find(quote, start + 1) < 0:
        return start, "unique_quote_offset_recovered"
    return None, "quote_missing_or_ambiguous"


def convert(raw):
    corpus, queries, qrels, audit = {}, {}, {}, Counter()
    unavailable = []
    for article in raw["data"]:
        for paragraph in article["paragraphs"]:
            did = str(article["id"]) + ":" + str(paragraph["id"])
            title, body = article["title"], paragraph["context"]
            if did in corpus:
                raise ValueError("duplicate source paragraph")
            corpus[did] = {"_id": did, "title": title, "text": body}
            sections = outline(body, title)
            revision = content_revision(title, body)
            for question in paragraph["qas"]:
                qid, spans = question["id"], {}
                if qid in queries:
                    raise ValueError("duplicate source question")
                for answer in question["answers"]:
                    start, status = anchor(body, answer)
                    audit[status] += 1
                    if start is None:
                        continue
                    end = start + len(answer["text"])
                    section = next((s for s in sections if
                        s["body_start"] <= start < end <= s["body_end"]), None)
                    if section is None:
                        audit["outside_representable_section_body"] += 1
                        continue
                    spans[(start, end)] = {"document_id": did, "revision": revision,
                        "section_schema": PARSER_VERSION, "section_id": section["section_id"],
                        "start": start - section["body_start"], "end": end - section["body_start"],
                        "quote": answer["text"], "group": "gold_answer_alternatives"}
                queries[qid] = {"_id": qid, "text": question["question"],
                    "scope_document_id": did, "evidence_groups": [[did]],
                    "category": "known_source_answer_span" if spans else "span_unavailable"}
                if spans:
                    queries[qid]["evidence_spans"] = list(spans.values())
                else:
                    unavailable.append(qid)
                qrels[qid] = {did: 2}
    return corpus, queries, qrels, {"annotations": dict(audit),
        "queries_without_representable_span": unavailable,
        "span_queries": len(queries) - len(unavailable)}


def prepare(source, output):
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SHA256:
        raise ValueError("changed pinned CMRC source")
    if output.exists():
        raise FileExistsError("choose a new output directory")
    corpus, queries, qrels, audit = convert(json.loads(raw))
    manifest = {"schema_version": 1, "name": "CMRC2018 dev known-source answer-span diagnostic",
        "source": SOURCE, "revision": REVISION, "source_sha256": SHA256,
        "license": "CC-BY-SA-4.0 as declared by the source repository; corpus not redistributed",
        "citation": "Cui et al., A Span-Extraction Dataset for Chinese Machine Reading "
                    "Comprehension, EMNLP-IJCNLP 2019, https://aclanthology.org/D19-1600/",
        "scope": "Known original paragraph supplied through scope_document_id. Not official "
                 "CMRC scoring, global retrieval, chapter-complete evidence or answer correctness. "
                 "All dev questions retained; unavailable spans excluded only from span metrics.",
        "offset_policy": "Validate original offset; recover only an exact unique answer quote. "
                         "Missing/ambiguous quotes or heading-only anchors are explicitly audited.",
        "audit": audit}
    (output / "qrels").mkdir(parents=True)
    for name, rows in (("corpus", corpus), ("queries", queries)):
        with (output / (name + ".jsonl")).open("w") as stream:
            for key in sorted(rows):
                stream.write(json.dumps(rows[key], ensure_ascii=False) + "\n")
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    with (output / "qrels/dev.tsv").open("w") as stream:
        stream.write("query-id\tcorpus-id\tscore\n")
        for qid in sorted(qrels):
            for did, grade in qrels[qid].items():
                stream.write(f"{qid}\t{did}\t{grade}\n")
    checked = load(output, "dev")
    return {"documents": len(corpus), "queries": len(queries), "audit": audit,
            "input_hashes": checked["hashes"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.output)))


if __name__ == "__main__":
    main()
