"""Synthetic offline evidence for exercising the review workflow, never a holdout."""

import json
import subprocess

from eval.improvement import candidates, datasets, feedback
from eval.improvement.artifacts import Registry, digest
from eval.online_baseline import snapshot
from eval.release_readiness import PROFILE


def case(name, tags=None):
    return {"id": name, "source_group": "fixture/" + name, "identity": "user",
            "tags": tags or ["chat"], "user_input": "Synthetic question " + name,
            "context": {}, "gold": {"nonempty": True, "forbid_business_write": True},
            "first_action": {"any_tools": [], "allow_text": True}}


def suite(path, cases):
    path.write_bytes(datasets.suite_bytes(cases))
    return path


def label(example, **updates):
    return {"decision": "confirmed_issue", "category": "persona", "severity": "medium",
            "reviewer": "synthetic fixture", "method": "code_audit",
            "rationale": "private reviewer notes must not reach optimizer",
            "source_group": example["source_group"], "evidence_quotes": ["wrong address"],
            "case": example, "optimization_goal": "Use the verified caller's address.",
            "model_export_approved": True, **updates}


def repository(path):
    path.mkdir()
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    for key, value in (("user.name", "test"), ("user.email", "test@example.invalid"),
                       ("commit.gpgsign", "false")):
        candidates.git(path, "config", key, value)
    for name in candidates.TARGETS:
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("ADDRESS = 'before'\n")
    candidates.git(path, "add", ".")
    candidates.git(path, "commit", "-m", "feat(test): 初始化合成夹具")
    return path


def setup(tmp_path, dev=None, held=None):
    registry = Registry(tmp_path / "registry")
    dev, held = dev or [case("development")], held or [case("unseen")]
    split = datasets.seal(registry, suite(tmp_path / "dev.jsonl", dev),
        suite(tmp_path / "held.jsonl", held), curator="fixture",
        independence_review="Synthetic test data, not independent production validation.")
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps({"reply": "wrong address", "private": "do not export source"}))
    review = feedback.review(registry, registry.evidence(path), label(dev[0]))
    repo = repository(tmp_path / "repo")
    base = candidates.commit(repo, "HEAD")
    packet = candidates.packet(registry, split, [review], repo, base)
    proposal = candidates.proposals(registry, packet, {"candidates": [{
        "title": "Fix address", "rationale": "Use verified identity", "edits": [{
            "path": candidates.TARGETS[0], "old": "'before'", "new": "'after'"}]}]})
    (repo / candidates.TARGETS[0]).write_text("ADDRESS = 'after'\n")
    candidates.git(repo, "add", ".")
    candidates.git(repo, "commit", "-m", "fix(test): 修正合成称呼")
    candidate = candidates.freeze(registry, proposal, 0, repo, "HEAD", "fixture reviewer")
    return registry, split, packet, proposal, candidate, repo


def full_cases(prefix, critical=False):
    tags = [["chat", "identity", "security", "no_implicit_approval", "isolation"],
            ["browser"], ["read"], ["body"], ["write_prepare"], ["confirmation"],
            ["memory"], ["multi_intent"]]
    cases = [case(f"{prefix}_{group}_{index}", tag)
             for group, tag in enumerate(tags) for index in range(20)]
    for index, item in enumerate(cases):
        item["identity"] = ["admin", "user", "zako"][index % 3]
    if critical:
        for item, name in zip(cases, json.loads(PROFILE.read_bytes())["critical_cases"]):
            item["id"] = name
    return cases


def report(tmp_path, cases, build, *, role="regression", improved=False, evaluation_id=None):
    tmp_path.mkdir()
    hashes = snapshot(tmp_path, cases)
    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps({"input": {"runtime": {"name": "native", "build_sha": build,
        "model": "synthetic"}}, "reply": "synthetic reply", "events": [
        {"node": "native", "event": "llm_start"},
        {"node": "native", "event": "llm_done", "input": 80
         if improved else 100, "output": 10, "cache_read": 70 if improved else 80,
         "tool_calls": [], "invalid_tool_calls": False}]}))
    rows = [{"id": item["id"], "identity": item["identity"], "tags": item["tags"],
             "first_action": item["first_action"], "status": "passed",
             "case_sha256": digest(json.dumps(item, sort_keys=True).encode()),
             "result": {"rounds": [{"trace": str(trace), "exec_rows": [], "http_timing": {
                 "trace_sha256": digest(trace.read_bytes()), "elapsed_s": 2,
                 "first_reply_s": 1, "first_visible_s": 0.1, "first_command_s": 0.2,
                 "first_confirmation_s": 0.2}}]}} for item in cases]
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"full_run": True, "planned": len(rows), "cases": rows,
        "service_version": {"sha": build}, "suite_sha256": digest(datasets.suite_bytes(cases)),
        "dataset_role": role, "evaluation_id": evaluation_id,
        "business_write_approval": False, "automatic_retries": 0,
        "evaluator_sha256": hashes}))
    review = tmp_path / "semantic.json"
    write_semantic(path, review)
    (tmp_path / "calibration.json").write_text(json.dumps({"model": "synthetic",
        "rubric_sha256": "frozen-synthetic-rubric", "source_sha256": "synthetic-cases",
        "rows": [{"expected_issue": True, "predicted_issue": True},
                 {"expected_issue": False, "predicted_issue": False}]}))
    return path, review


def write_semantic(report_path, path, suspect=()):
    path.write_text(json.dumps({"source_report_sha256": digest(report_path.read_bytes()),
        "rubric_sha256": "frozen-synthetic-rubric", "evaluator_sha256": {"judge": "fixture"},
        "model": "synthetic", "rows": [{"case": row["id"], "round": 1,
            "status": "suspect" if row["id"] in suspect else "no_issue_found"}
            for row in json.loads(report_path.read_bytes())["cases"]]}))
