"""Review signals against frozen evidence before exporting a curated reproduction."""

import json
import tempfile
from pathlib import Path

from eval.improvement.artifacts import digest, nonempty
from eval.online_baseline import load_suite

DECISIONS = {"confirmed_issue", "not_issue", "insufficient_evidence"}
CATEGORIES = {"intent", "evidence", "persona", "tool", "context", "cache", "latency", "other"}


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def validate_cases(cases):
    with tempfile.TemporaryDirectory(prefix="saudade-reviewed-cases-") as folder:
        path = Path(folder) / "cases.jsonl"
        path.write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases))
        loaded = load_suite(path)
    for case in loaded:
        rounds = case.get("rounds", [case])
        if any(not rnd.get("gold", case.get("gold")) for rnd in rounds):
            raise ValueError("reviewed cases need expected-outcome assertions")
    return loaded


def review(registry, evidence, label):
    source = registry.get(evidence, "evidence")
    if digest(source["json"].encode()) != source["sha256"]:
        raise ValueError("frozen source digest mismatch")
    decision = label.get("decision")
    if decision not in DECISIONS or label.get("category") not in CATEGORIES:
        raise ValueError("unknown review decision or category")
    if label.get("severity") not in {"low", "medium", "high", "critical"}:
        raise ValueError("unknown severity")
    if label.get("method") not in {"human", "code_audit"}:
        raise ValueError("model verdicts are signals, not reviewed labels")
    for name in ("reviewer", "rationale", "source_group"):
        nonempty(label.get(name), name)
    quotes = label.get("evidence_quotes")
    if not isinstance(quotes, list) or not quotes:
        raise ValueError("review needs supporting evidence quotes")
    material = list(strings(json.loads(source["json"])))
    if any(not isinstance(quote, str) or not quote.strip()
           or not any(quote in item for item in material) for quote in quotes):
        raise ValueError("supporting quote is absent from frozen evidence")
    approved = label.get("model_export_approved", False)
    if type(approved) is not bool:
        raise ValueError("model export approval must be explicit boolean")
    if approved and decision != "confirmed_issue":
        raise ValueError("only confirmed issues can supply optimizer examples")
    case = label.get("case")
    if case is not None:
        validate_cases([case])
    if approved:
        if case is None:
            raise ValueError("model export needs an anonymized reproducible case")
        nonempty(label.get("optimization_goal"), "optimization_goal")
    return registry.put("feedback", {"evidence": evidence, "label": label})


def model_example(registry, key):
    label = registry.get(key, "feedback")["label"]
    if label["decision"] != "confirmed_issue" or label.get("model_export_approved") is not True:
        raise ValueError("feedback is not approved for model export")
    # Reviewer rationale, source quotes and raw traces may contain private data.
    return {"case": label["case"], "goal": label["optimization_goal"]}
