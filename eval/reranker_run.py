"""One bounded ONNX Runtime cross-encoder; append-only per-query offline checkpoints."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import time
from pathlib import Path

os.environ["SAUDADE_IGNORE_ENV_FILE"] = "1"

import numpy as np  # noqa: E402

from eval.reranker_inputs import PAIR_LIMIT, input_digest, sha256  # noqa: E402


class CrossEncoder:
    def __init__(self, model, threads=2, *, bounded_memory=False):
        import onnxruntime as ort

        options = ort.SessionOptions()
        if bounded_memory:
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
            options.enable_cpu_mem_arena = False
            options.enable_mem_pattern = False
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        options.add_session_config_entry("session.inter_op.allow_spinning", "0")
        self.session = ort.InferenceSession(str(model), options,
                                           providers=["CPUExecutionProvider"])
        self.names = {item.name for item in self.session.get_inputs()}
        if not {"input_ids", "attention_mask"} <= self.names <= {
                "input_ids", "attention_mask", "token_type_ids"}:
            raise ValueError("unexpected cross-encoder input contract")

    def score(self, pairs):
        width = max(len(pair["input_ids"]) for pair in pairs)
        if not 0 < width <= PAIR_LIMIT or any(not p["input_ids"] for p in pairs):
            raise ValueError("empty or over-budget cross-encoder input")
        ids = np.full((len(pairs), width), 1, dtype=np.int64)
        mask = np.zeros_like(ids)
        for index, pair in enumerate(pairs):
            size = len(pair["input_ids"])
            ids[index, :size] = pair["input_ids"]
            mask[index, :size] = 1
        values = {"input_ids": ids, "attention_mask": mask, "token_type_ids": np.zeros_like(ids)}
        output = self.session.run(None, {key: values[key] for key in self.names})[0]
        scores = np.asarray(output).reshape(-1)
        if len(scores) != len(pairs) or not np.isfinite(scores).all():
            raise ValueError("nonfinite or incomplete cross-encoder scores")
        return scores.tolist(), int(ids.size)


def cached_rows(path):
    result = {}
    if not path.exists():
        return result
    with path.open() as stream:
        for line in stream:
            row = json.loads(line)
            if row["query_id"] in result:
                raise ValueError("duplicate checkpoint query")
            result[row["query_id"]] = row
    return result


def score_query(model, row, batch_size):
    started, cpu = time.monotonic(), time.process_time()
    scores, padded, calls = [], 0, 0
    for start in range(0, len(row["candidates"]), batch_size):
        batch = row["candidates"][start:start + batch_size]
        values, tokens = model.score(batch)
        scores.extend(values)
        padded += tokens
        calls += 1
    return {"query_id": row["query_id"], "input_sha256": input_digest(row),
            "scores": [{"document_id": pair["document_id"], "logit": value}
                       for pair, value in zip(row["candidates"], scores, strict=True)],
            "seconds": time.monotonic() - started, "cpu_seconds": time.process_time() - cpu,
            "model_batches": calls, "padded_tokens": padded,
            "unpadded_tokens": sum(p["input_tokens"] for p in row["candidates"])}


def run(model, source, output, *, batch_size=4, limit=0):
    cached = cached_rows(output)
    seen, computed = set(), 0
    with gzip.open(source, "rt") as stream, output.open("a") as sink:
        for line in stream:
            row = json.loads(line)
            qid = row["query_id"]
            if qid in seen:
                raise ValueError("duplicate input query")
            seen.add(qid)
            if qid in cached:
                saved = cached[qid]
                if (saved["input_sha256"] != input_digest(row)
                        or [p["document_id"] for p in row["candidates"]]
                        != [p["document_id"] for p in saved["scores"]]
                        or any(not math.isfinite(p["logit"]) for p in saved["scores"])):
                    raise ValueError("checkpoint input/candidates/scores changed")
                continue
            result = score_query(model, row, batch_size)
            sink.write(json.dumps(result) + "\n")
            sink.flush()
            os.fsync(sink.fileno())
            computed += 1
            if computed % 25 == 0 or computed == 1:
                print(json.dumps({"new_queries": computed, "cached_queries": len(cached),
                                  "last_seconds": result["seconds"]}), flush=True)
            if limit and computed >= limit:
                return {"complete": False, "new_queries": computed, "cached_queries": len(cached)}
    if cached.keys() - seen:
        raise ValueError("foreign checkpoint query")
    return {"complete": True, "new_queries": computed, "cached_queries": len(cached)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    if args.batch_size < 1 or args.threads < 1 or args.limit < 0:
        parser.error("positive batch/threads and nonnegative limit required")
    binding = {"input_sha256": sha256(args.inputs), "model_sha256": sha256(args.model),
               "batch_size": args.batch_size, "threads": args.threads}
    config = args.output.with_suffix(".config.json")
    if args.output.exists() and args.output.stat().st_size and not config.exists():
        raise ValueError("checkpoint has no model/input binding")
    if config.exists() and json.loads(config.read_text()) != binding:
        raise ValueError("frozen model/input/runtime configuration changed")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(binding, indent=2) + "\n")
    started = time.monotonic()
    model = CrossEncoder(args.model, args.threads)
    loaded = time.monotonic() - started
    with gzip.open(args.inputs, "rt") as stream:
        sample = json.loads(next(stream))["candidates"][:args.batch_size]
    warming = time.monotonic()
    _, warmup_padded = model.score(sample)
    warmup_seconds = time.monotonic() - warming
    result = run(model, args.inputs, args.output, batch_size=args.batch_size, limit=args.limit)
    result.update(model_load_seconds=loaded, warmup_seconds=warmup_seconds,
                  warmup_pairs=len(sample), warmup_padded_tokens=warmup_padded,
                  output_sha256=sha256(args.output),
                  scope="Pretokenized query-by-query CPU replay; not online end-to-end latency.",
                  cloud_calls=0, semantic_judge_calls=0)
    args.output.with_suffix(".progress.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
