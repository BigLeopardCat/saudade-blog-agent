"""Small fixed-input representation control, separate from the full INT8 quality run."""

import argparse
import gzip
import json
import time
from pathlib import Path

from eval.reranker_inputs import sha256
from eval.reranker_run import CrossEncoder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--kind", choices=["fp32", "int8"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("do not overwrite a fixed representation control")
    with gzip.open(args.inputs, "rt") as stream:
        pairs = [json.loads(line) for line in stream]
    started = time.monotonic()
    model = CrossEncoder(args.model, 2, bounded_memory=args.kind == "fp32")
    loaded = time.monotonic() - started
    scores, seconds = [], []
    for pair in pairs:
        started = time.monotonic()
        value, _ = model.score([pair])
        seconds.append(time.monotonic() - started)
        scores.extend(value)
    result = {"kind": args.kind, "model_sha256": sha256(args.model),
              "input_sha256": sha256(args.inputs), "model_load_seconds": loaded,
              "scores": scores, "seconds": seconds, "pairs": len(pairs),
              "positive_above_negative": scores[-2] > scores[-1],
              "scope": "Fixed public pairs and synthetic orientation check. "
                       "Author FP32 uses basic graph optimization without memory arena. "
                       "This is not full-corpus FP32 quality or exhaustive conversion equivalence."}
    if args.kind == "int8":
        values, _ = model.score(pairs[-4:])
        result["last4_batched_scores"] = values
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    if not result["positive_above_negative"]:
        raise ValueError("cross-encoder score orientation control failed")
    print(json.dumps({k: v for k, v in result.items() if k not in {"scores", "seconds"}}))


if __name__ == "__main__":
    main()
