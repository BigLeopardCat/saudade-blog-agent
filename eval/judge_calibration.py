"""Calibrate the semantic judge on labeled synthetic materials, without business calls."""

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

from openai import OpenAIError

from eval import online_judge, semantic_judge
from eval.statistical_metrics import proportion
from models.errors import error_fields

SAMPLES = Path(__file__).parent / "golden/judge_calibration.jsonl"


def load_samples(path=SAMPLES):
    samples = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    if not samples or len({sample["id"] for sample in samples}) != len(samples):
        raise ValueError("calibration samples must be nonempty and unique")
    if any(type(sample["expected_issue"]) is not bool or not sample.get("rationale")
           or not isinstance(sample.get("material"), dict) for sample in samples):
        raise ValueError("each calibration sample needs a boolean label, rationale and material")
    return samples


def summarize(rows):
    known = [row for row in rows if row.get("predicted_issue") is not None]
    tp = sum(row["expected_issue"] and row["predicted_issue"] for row in known)
    fp = sum(not row["expected_issue"] and row["predicted_issue"] for row in known)
    fn = sum(row["expected_issue"] and not row["predicted_issue"] for row in known)
    tn = sum(not row["expected_issue"] and not row["predicted_issue"] for row in known)
    return {"samples": len(rows), "measured": len(known), "unverified": len(rows) - len(known),
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": proportion(tp, tp + fp), "recall": proportion(tp, tp + fn),
            "false_positive_rate": proportion(fp, fp + tn),
            "scope": "labeled synthetic calibration; not production judge accuracy"}


def evaluate(samples, llm, *, on_progress=None):
    rows = []
    for sample in samples:
        row = {"id": sample["id"], "expected_issue": sample["expected_issue"],
               "predicted_issue": None, "diagnostics": {}, "attempt_status": "started"}
        text = json.dumps(sample["material"], ensure_ascii=False)
        rows.append(row)
        if on_progress is not None:
            on_progress(rows)
        try:
            verdict = semantic_judge.judge_one(
                text, llm, instructions=online_judge.INSTRUCTIONS, diagnostics=row["diagnostics"])
            online_judge.validate_excerpts(verdict, text)
            row.update(predicted_issue=bool(verdict["unsupported"] or not verdict["answered"]),
                       verdict=verdict)
        except (OpenAIError, ValueError, TypeError, KeyError) as exc:
            facts = error_fields(exc)
            if facts["failure_source"] in {"provider", "transport"}:
                row.update(error=type(exc).__name__, error_fields=facts, attempt_status="failed")
                if on_progress is not None:
                    on_progress(rows)
                raise
            row["error"] = f"{type(exc).__name__}: {exc}"
        row["attempt_status"] = "completed"
        if on_progress is not None:
            on_progress(rows)
    return {"rows": rows, **summarize(rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, default=SAMPLES)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reasoning-effort", choices=semantic_judge.REASONING_EFFORTS,
                        default="none")
    parser.add_argument("--max-output-tokens", type=int,
                        help="Explicit judge output budget including reasoning; defaults unchanged")
    args = parser.parse_args()
    if args.max_output_tokens is not None and args.max_output_tokens <= 0:
        parser.error("--max-output-tokens must be positive")
    samples = load_samples(args.samples)
    args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    body = args.samples.read_bytes()
    (args.out / "samples.jsonl").write_bytes(body)
    hashes = online_judge.snapshot_reviewer(args.out)
    driver = Path(__file__).read_bytes()
    (args.out / Path(__file__).name).write_bytes(driver)
    hashes[Path(__file__).name] = hashlib.sha256(driver).hexdigest()
    report = {"source_sha256": hashlib.sha256(body).hexdigest(),
              "evaluator_sha256": hashes,
              "rubric_sha256": hashlib.sha256(online_judge.INSTRUCTIONS.encode()).hexdigest(),
              "business_requests": 0, "automatic_retries": 0,
              "dataset_role": "calibration_development",
              "backend": semantic_judge.backend_identity(),
              "independent_validation": "not_evaluated",
              "planned_samples": len(samples), "status": "running", "rows": [],
              "started_at": datetime.now().astimezone().isoformat()}

    def save(rows=None):
        if rows is not None:
            report.update(rows=rows, **summarize(rows))
        temporary = args.out / "report.tmp"
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(args.out / "report.json")

    save()
    if args.dry_run:
        report.update(status="not_evaluated", samples=len(samples))
    else:
        try:
            llm = semantic_judge.make_llm(
                reasoning_effort=args.reasoning_effort,
                **({"max_tokens": args.max_output_tokens}
                   if args.max_output_tokens is not None else {}),
                timeout=60 if args.reasoning_effort != "none" else 30)
            report.update(model=llm.model_name,
                          reviewer_options=semantic_judge.reviewer_options(llm))
            report.update(**evaluate(samples, llm, on_progress=save), status="completed")
        except BaseException as exc:
            report.update(status="failed" if isinstance(exc, Exception) else "interrupted",
                          error_type=type(exc).__name__,
                          finished_at=datetime.now().astimezone().isoformat())
            save(report["rows"])
            raise
    report["finished_at"] = datetime.now().astimezone().isoformat()
    save()
    print(json.dumps({key: value for key, value in report.items() if key != "rows"},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
