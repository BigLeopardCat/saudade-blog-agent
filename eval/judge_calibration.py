"""Calibrate the semantic judge on labeled synthetic materials, without business calls."""

import argparse
import hashlib
import json
from pathlib import Path

from openai import OpenAIError

from eval import llm_judge, online_judge
from eval.statistical_metrics import proportion

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


def evaluate(samples, llm):
    rows = []
    for sample in samples:
        row = {"id": sample["id"], "expected_issue": sample["expected_issue"],
               "predicted_issue": None}
        text = json.dumps(sample["material"], ensure_ascii=False)
        try:
            verdict, _ = llm_judge.judge_one(text, llm, instructions=online_judge.INSTRUCTIONS,
                                            allow_fallback=False)
            online_judge.validate_excerpts(verdict, text)
            row.update(predicted_issue=bool(verdict["unsupported"] or not verdict["answered"]),
                       verdict=verdict)
        except (OpenAIError, ValueError, TypeError, KeyError) as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        rows.append(row)
    return {"rows": rows, **summarize(rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, default=SAMPLES)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    samples = load_samples(args.samples)
    args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    body = args.samples.read_bytes()
    (args.out / "samples.jsonl").write_bytes(body)
    report = {"source_sha256": hashlib.sha256(body).hexdigest(),
              "rubric_sha256": hashlib.sha256(online_judge.INSTRUCTIONS.encode()).hexdigest(),
              "business_requests": 0, "automatic_retries": 0,
              "dataset_role": "calibration_development",
              "independent_validation": "not_evaluated"}
    if args.dry_run:
        report.update(status="not_evaluated", samples=len(samples))
    else:
        llm = llm_judge.make_llm(max_retries=0, streaming=False, timeout=30)
        report.update(model=llm.model_name, **evaluate(samples, llm))
    (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "rows"},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
