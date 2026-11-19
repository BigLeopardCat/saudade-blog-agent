"""Only matched, complete evidence can hand a frozen candidate to release review."""

import json

import pytest
from _improvement_support import full_cases, report, setup, write_semantic

from eval.improvement import datasets, holdout, release


@pytest.fixture
def evaluated(tmp_path):
    dev, held = full_cases("dev", critical=True), full_cases("held")
    registry, split, _, _, candidate, _ = setup(tmp_path, dev, held)
    frozen = registry.get(candidate, "candidate")
    before, old_review = report(tmp_path / "before", dev, frozen["base"])
    after, new_review = report(tmp_path / "after", dev, frozen["head"], improved=True)
    return registry, candidate, before, after, old_review, new_review, held, frozen


def development(evaluated):
    registry, candidate, before, after, old_review, new_review, *_ = evaluated
    return release.development(registry, candidate, before, after, old_review, new_review,
        reviewer="fixture review", comparison_conditions="same synthetic cache conditions",
        calibration_path=after.parent / "calibration.json")


def finished_holdout(evaluated, tmp_path, monkeypatch):
    registry, _, _, _, _, _, held, frozen = evaluated
    gate = development(evaluated)
    assert registry.get(gate, "development")["status"] == "passed"
    reservation = datasets.reserve(registry, gate)

    def fake(cases, transport, identities, folder, **kwargs):
        assert cases == held and kwargs["expected_build"] == frozen["head"]
        report(folder, cases, frozen["head"], role="holdout", improved=True,
               evaluation_id=kwargs["evaluation_id"])

    monkeypatch.setattr(holdout.online_baseline, "run", fake)
    holdout.run(registry, reservation, None, {}, tmp_path / "holdout")
    return reservation, tmp_path / "holdout/report.json", tmp_path / "holdout/semantic.json"


def test_complete_offline_handoff_keeps_deploy_separate(evaluated, tmp_path, monkeypatch):
    registry, _, _, _, _, _, _, frozen = evaluated
    reservation, run, review = finished_holdout(evaluated, tmp_path, monkeypatch)
    packet = release.release_packet(registry, reservation, run, review, reviewer="fixture")
    got = registry.get(packet, "release_packet")
    assert got["status"] == "ready_for_review"
    assert got["head"] == frozen["head"] and got["rollback_base"] == frozen["base"]
    assert got["separate_evidence_required"]
    assert got["holdout_statistics"]["total"] == 160
    # Archiving retains evidence when ephemeral runner folders are removed later.
    run.unlink()
    assert registry.get(got["holdout_evidence"]["report"], "evidence")["sha256"]


@pytest.mark.parametrize("field", ["backend", "reviewer_options", "evaluator_sha256"])
@pytest.mark.parametrize("target", ["calibration", "paired", "holdout"])
def test_backend_and_inference_identity_cannot_change_across_review_phases(
        evaluated, tmp_path, monkeypatch, field, target):
    _, _, _, after, _, review, *_ = evaluated
    if target == "holdout":
        reservation, path, review = finished_holdout(evaluated, tmp_path, monkeypatch)
    else:
        path = after
        if target == "calibration":
            review = after.parent / "calibration.json"
    document = json.loads(review.read_bytes())
    document[field] = {"changed": "identity"}
    review.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="calibration|reviewer|evaluator"):
        if target == "holdout":
            release.release_packet(evaluated[0], reservation, path, review, reviewer="fixture")
        else:
            development(evaluated)


def test_old_reports_without_backend_identity_cannot_certify_a_current_candidate(evaluated):
    _, _, _, after, before_review, after_review, *_ = evaluated
    for path in (before_review, after_review, after.parent / "calibration.json"):
        document = json.loads(path.read_bytes())
        document.pop("backend")
        document.pop("reviewer_options")
        path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="frozen evaluator"):
        development(evaluated)


@pytest.mark.parametrize("fault", ["contract_regression", "cost_regression", "missing_usage",
                                 "wrong_candidate", "changed_evaluator", "unfinished_call"])
def test_bad_development_cannot_open_holdout(evaluated, fault):
    registry, _, _, after, _, review, *_ = evaluated
    doc = json.loads(after.read_bytes())
    if fault == "wrong_candidate":
        doc["service_version"]["sha"] = "other"
    elif fault == "contract_regression":
        doc["cases"][0]["contract_status"] = "failed"
    elif fault == "changed_evaluator":
        doc["evaluator_sha256"]["golden_evaluation.py"] = "changed"
    elif fault in {"cost_regression", "missing_usage", "unfinished_call"}:
        trace = release.Path(doc["cases"][0]["result"]["rounds"][0]["trace"])
        body = json.loads(trace.read_bytes())
        if fault == "unfinished_call":
            body["events"].append({"node": "native", "event": "llm_start"})
        elif fault == "missing_usage":
            body["events"][-1].pop("cache_read")
        else:
            body["events"][-1]["input"] = 150
        trace.write_text(json.dumps(body))
        # An intact but worse run should also fail, not merely trace tampering.
        for case in doc["cases"]:
            case["result"]["rounds"][0]["http_timing"]["trace_sha256"] = (
                release.digest(trace.read_bytes()))
    after.write_text(json.dumps(doc))
    write_semantic(after, review)
    try:
        gate = development(evaluated)
    except ValueError:
        return
    assert registry.get(gate, "development")["status"] == "blocked"
    with pytest.raises(ValueError, match="not passed"):
        datasets.reserve(registry, gate)


def test_holdout_exposure_is_single_use_even_after_interrupted_run(evaluated):
    registry, *_ = evaluated
    gate = development(evaluated)
    datasets.reserve(registry, gate)
    with pytest.raises(FileExistsError):
        datasets.reserve(registry, gate)


def test_partly_reused_holdout_cannot_be_reopened_under_a_new_dataset_id(evaluated):
    registry, _, _, _, _, _, _, frozen = evaluated
    gate_id = development(evaluated)
    datasets.reserve(registry, gate_id)
    original = registry.get(frozen["datasets"], "datasets")
    # Simulate another curator's valid new artifact with a partly reused group.
    original["exposure_key"] = "f" * 64
    replacement = registry.put("datasets", original)
    candidate = registry.put("candidate", {**frozen, "datasets": replacement})
    gate = registry.get(gate_id, "development")
    gate_id = registry.put("development", {**gate, "candidate": candidate})
    with pytest.raises(FileExistsError):
        datasets.reserve(registry, gate_id)


@pytest.mark.parametrize("fault", ["wrong_build", "wrong_role", "contract_failure",
                                 "missing_contract", "altered_trace", "changed_judge"])
def test_holdout_failure_blocks_release(evaluated, tmp_path, monkeypatch, fault):
    registry = evaluated[0]
    reservation, path, review = finished_holdout(evaluated, tmp_path, monkeypatch)
    doc = json.loads(path.read_bytes())
    if fault == "wrong_build":
        doc["service_version"]["sha"] = "other"
    elif fault == "wrong_role":
        doc["dataset_role"] = "regression"
    elif fault == "contract_failure":
        doc["cases"][0]["contract_status"] = "failed"
    elif fault == "missing_contract":
        del doc["cases"][0]["contract_status"]
    elif fault == "altered_trace":
        release.Path(doc["cases"][0]["result"]["rounds"][0]["trace"]).write_text("{}")
    path.write_text(json.dumps(doc))
    write_semantic(path, review)
    semantic = json.loads(review.read_bytes())
    if fault == "changed_judge":
        semantic["model"] = "other"
    review.write_text(json.dumps(semantic))
    try:
        key = release.release_packet(registry, reservation, path, review, reviewer="fixture")
    except ValueError:
        return
    assert registry.get(key, "release_packet")["status"] == "blocked"


def test_failed_or_interrupted_holdout_cannot_be_rerun(evaluated, tmp_path, monkeypatch):
    registry, *_ = evaluated
    reservation = datasets.reserve(registry, development(evaluated))
    calls = []

    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("synthetic interruption")

    monkeypatch.setattr(holdout.online_baseline, "run", fail)
    with pytest.raises(RuntimeError):
        holdout.run(registry, reservation, None, {}, tmp_path / "interrupted")
    with pytest.raises(FileExistsError):
        holdout.run(registry, reservation, None, {}, tmp_path / "retry")
    assert len(calls) == 1



def test_semantic_regressions_are_preserved_without_blocking_review(evaluated, tmp_path, monkeypatch):
    registry, _, _, after, _, semantic, *_ = evaluated
    case = json.loads(after.read_bytes())["cases"][0]["id"]
    write_semantic(after, semantic, suspect={case})
    gate = registry.get(development(evaluated), "development")
    assert gate["status"] == "passed"
    assert gate["semantic_regressions"] == [case]

    reservation, path, review = finished_holdout(evaluated, tmp_path, monkeypatch)
    holdout_case = json.loads(path.read_bytes())["cases"][0]["id"]
    write_semantic(path, review, suspect={holdout_case})
    packet = release.release_packet(registry, reservation, path, review, reviewer="fixture")
    got = registry.get(packet, "release_packet")
    assert got["status"] == "ready_for_review"
    assert got["holdout_statistics"]["passed"] == 159
    assert got["holdout_contract_statistics"]["passed"] == 160
