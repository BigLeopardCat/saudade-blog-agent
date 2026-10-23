"""Measure an explicitly selected embedding endpoint; this is not a quality evaluation."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from importlib.metadata import version
from pathlib import Path

from openai import APIError, OpenAI


def memory(pid: int | None) -> dict:
    if pid is None:
        return {}
    values = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in ("VmRSS", "VmHWM"):
            values[key + "_KiB"] = int(value.split()[0])
    return values


def probe(
    client: OpenAI, model: str, cases: list[dict], repeat: int, dimensions: int, pid: int | None
) -> list[dict]:
    results = []
    for iteration in range(repeat):
        for case in cases:
            row = {"id": case["_id"], "iteration": iteration, "characters": len(case["text"])}
            start = time.perf_counter()
            try:
                response = client.embeddings.create(
                    model=model, input=case["text"], encoding_format="float"
                )
                if len(response.data) != 1 or response.data[0].index != 0:
                    raise ValueError("invalid embedding response cardinality/index")
                vector = response.data[0].embedding
                if len(vector) != dimensions or not all(math.isfinite(x) for x in vector):
                    raise ValueError("invalid embedding dimension or non-finite values")
                norm = math.sqrt(sum(x * x for x in vector))
                if not math.isfinite(norm) or norm <= 0:
                    raise ValueError("invalid embedding norm")
                row.update(
                    ok=True,
                    dimensions=len(vector),
                    norm=norm,
                    prompt_tokens=response.usage.prompt_tokens,
                )
            except APIError as error:
                # No error body/URL: API gateways may include credentials or input text there.
                row.update(
                    ok=False, error=type(error).__name__, status=getattr(error, "status_code", None)
                )
            row.update(seconds=time.perf_counter() - start, memory=memory(pid))
            results.append(row)
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--input", required=True, type=Path, help="JSONL: _id and exact input text")
    parser.add_argument(
        "--info", required=True, type=Path, help="name/revision/config JSON, no secrets"
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--api-key-env", default="EMBEDDING_API_KEY")
    parser.add_argument("--expected-dimensions", required=True, type=int)
    parser.add_argument("--repeat", default=3, type=int)
    parser.add_argument("--pid", type=int, help="optional local server PID for Linux RSS/HWM")
    args = parser.parse_args(argv)
    if not 1 <= args.repeat <= 20 or args.expected_dimensions < 1:
        parser.error("repeat must be 1..20 and expected dimensions must be positive")
    cases = [json.loads(line) for line in args.input.read_text().splitlines() if line.strip()]
    if (
        not cases
        or any(
            not isinstance(c, dict)
            or not isinstance(c.get("_id"), str)
            or not c["_id"]
            or not isinstance(c.get("text"), str)
            or not c["text"].strip()
            for c in cases
        )
        or len({c["_id"] for c in cases}) != len(cases)
    ):
        parser.error("input requires unique string _id and nonempty text")
    info = json.loads(args.info.read_text())
    if not isinstance(info, dict) or any(not info.get(k) for k in ("name", "revision", "config")):
        parser.error("info requires name/revision/config")
    with OpenAI(
        base_url=args.base_url,
        api_key=os.getenv(args.api_key_env) or "local-only",
        timeout=60,
        max_retries=0,
    ) as client:
        rows = probe(client, args.model, cases, args.repeat, args.expected_dimensions, args.pid)
    result = {
        "schema_version": 1,
        "scope": "service latency/shape only; not retrieval quality",
        "model": args.model,
        "info": info,
        "openai_version": version("openai"),
        "input_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "results": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"requests": len(rows), "succeeded": sum(r["ok"] for r in rows)}))
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
