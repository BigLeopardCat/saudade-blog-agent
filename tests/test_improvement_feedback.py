"""Unreviewed or ungrounded signals must not become optimizer truth."""

import json

import pytest
from _improvement_support import case, label, setup, suite

from eval.improvement import candidates, datasets, feedback
from eval.improvement.artifacts import Registry


def test_evidence_is_frozen_and_integrity_checked(tmp_path):
    registry = Registry(tmp_path / "registry")
    source = tmp_path / "source.json"
    source.write_text('{"reply":"wrong address"}')
    key = registry.evidence(source)
    source.unlink()
    assert json.loads(registry.get(key, "evidence")["json"])["reply"] == "wrong address"
    target = registry.root / "artifacts" / (key + ".json")
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="integrity"):
        registry.get(key, "evidence")


@pytest.mark.parametrize("update", [
    {"method": "llm_judge"}, {"evidence_quotes": ["a statement never observed"]},
    {"reviewer": ""}, {"decision": "insufficient_evidence"},
    {"case": None}, {"model_export_approved": "yes"},
])
def test_unreliable_labels_cannot_be_exported(tmp_path, update):
    registry = Registry(tmp_path / "registry")
    source = tmp_path / "source.json"
    source.write_text('{"reply":"wrong address"}')
    with pytest.raises(ValueError):
        feedback.review(registry, registry.evidence(source), label(case("dev"), **update))


def test_uncertainty_is_preserved_without_becoming_training_material(tmp_path):
    registry = Registry(tmp_path / "registry")
    source = tmp_path / "source.json"
    source.write_text('{"reply":"wrong address"}')
    key = feedback.review(registry, registry.evidence(source), label(case("dev"),
        decision="insufficient_evidence", model_export_approved=False))
    assert registry.get(key, "feedback")["label"]["decision"] == "insufficient_evidence"
    with pytest.raises(ValueError, match="not approved"):
        feedback.model_example(registry, key)


def test_optimizer_input_excludes_raw_evidence_notes_and_holdout(tmp_path):
    registry, _, packet, *_ = setup(tmp_path)
    exported = registry.get(packet, "optimizer_input")["exported"]
    text = json.dumps(exported)
    assert "Synthetic question development" in text
    assert all(secret not in text for secret in
               ("do not export source", "private reviewer notes", "Synthetic question unseen"))


@pytest.mark.parametrize("field", ["id", "user_input", "source_group"])
def test_dataset_overlap_is_rejected_even_with_different_case_names(tmp_path, field):
    registry = Registry(tmp_path / "registry")
    dev, held = case("dev"), case("held")
    held[field] = dev[field]
    with pytest.raises(ValueError, match="overlap"):
        datasets.seal(registry, suite(tmp_path / "dev.jsonl", [dev]),
            suite(tmp_path / "held.jsonl", [held]), curator="test", independence_review="reviewed")


def test_request_overlap_ignores_case_and_whitespace(tmp_path):
    registry = Registry(tmp_path / "registry")
    dev, held = case("dev"), case("held")
    held["user_input"] = dev["user_input"].upper().replace(" ", " \n")
    with pytest.raises(ValueError, match="overlap"):
        datasets.seal(registry, suite(tmp_path / "dev.jsonl", [dev]),
            suite(tmp_path / "held.jsonl", [held]), curator="test", independence_review="reviewed")


def test_unreviewed_draft_never_enters_frozen_datasets(tmp_path):
    registry = Registry(tmp_path / "registry")
    dev = {**case("dev"), "_draft": {}}
    with pytest.raises(ValueError, match="draft"):
        datasets.seal(registry, suite(tmp_path / "dev.jsonl", [dev]),
            suite(tmp_path / "held.jsonl", [case("held")]),
            curator="test", independence_review="reviewed")


def test_model_generated_edits_are_bounded_and_never_applied(tmp_path):
    registry, _, packet, proposal, _, repo = setup(tmp_path)
    assert (repo / candidates.TARGETS[0]).read_text() == "ADDRESS = 'after'\n"
    result = registry.get(proposal, "proposals")
    result["candidates"][0]["edits"][0]["path"] = "agent/authz.py"
    result["candidates"][0].pop("patch")
    with pytest.raises(ValueError, match="outside scope"):
        candidates.proposals(registry, packet, {"candidates": result["candidates"]})


def test_candidate_cannot_include_unrelated_evaluator_changes(tmp_path):
    registry, _, _, proposal, _, repo = setup(tmp_path)
    path = repo / "eval.py"
    path.write_text("weaken = True\n")
    candidates.git(repo, "add", ".")
    candidates.git(repo, "commit", "-m", "test(test): 增加不允许的夹具文件")
    with pytest.raises(ValueError, match="outside proposal scope"):
        candidates.freeze(registry, proposal, 0, repo, "HEAD", "reviewer")
